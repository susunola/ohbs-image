from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import socket
import time
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ._ancestry import descendants, impact_plan
from ._approvals import approve, consume_approval, create_approval
from ._channels import promote_channel, resolve_channel
from ._config import _lineage_path
from ._console import CONSOLE_CSS, CONSOLE_HTML, CONSOLE_JS
from ._distribution import execute_distribution
from ._identity import IdentityError, verify_oidc_token
from ._logging import fail, info
from ._metrics import collect_metrics, prometheus_metrics
from ._policy_registry import list_policies, resolve_policy
from ._policy_simulation import simulate_policy
from ._rebuild_events import EVENT_SCHEMA, process_rebuild_event
from ._registry import _database, _hash, _read_object, change_artifact_status, get_artifact, put_artifact
from ._reports import _state_lock
from ._runs import collect_runs
from ._telemetry import TraceRecorder, parse_traceparent


class AuthorizationError(ValueError):
    pass


class ControlPlane:
    def __init__(self, root: Path, rbac_path: Path, *, rate_limit: int = 120,
                 rate_window_seconds: int = 60) -> None:
        self.root = root
        self.rbac_path = rbac_path
        self.rate_limit = rate_limit
        self.rate_window_seconds = rate_window_seconds
        self._requests: dict[str, deque[float]] = {}
        self._rbac_mtime_ns = -1
        self.tokens: dict[str, dict[str, Any]] = {}
        self.oidc: dict[str, Any] = {}
        self.approval_policy: dict[str, Any] = {}
        self._load_rbac(force=True)

    def _load_rbac(self, *, force: bool = False) -> None:
        try:
            mtime_ns = self.rbac_path.stat().st_mtime_ns
            if not force and mtime_ns == self._rbac_mtime_ns:
                return
            rbac = json.loads(self.rbac_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid RBAC configuration {self.rbac_path}") from exc
        tokens = rbac.get("tokens") if isinstance(rbac, dict) else None
        oidc = rbac.get("oidc") if isinstance(rbac, dict) else None
        if not isinstance(tokens, dict) and not isinstance(oidc, dict):
            raise ValueError("RBAC configuration requires tokens or oidc")
        self.tokens = {
            str(token): principal for token, principal in (tokens or {}).items()
            if isinstance(principal, dict)}
        self.oidc = oidc if isinstance(oidc, dict) else {}
        policy = rbac.get("approvals") if isinstance(rbac, dict) else None
        self.approval_policy = policy if isinstance(policy, dict) else {}
        self._rbac_mtime_ns = mtime_ns

    def principal(self, authorization: str) -> dict[str, Any]:
        self._load_rbac()
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise AuthorizationError("missing bearer token")
        supplied = authorization[len(prefix):]
        for token, principal in self.tokens.items():
            if hmac.compare_digest(supplied, token):
                expires_at = principal.get("expires_at")
                if expires_at:
                    try:
                        expires = datetime.fromisoformat(
                            str(expires_at).replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise AuthorizationError("token expiry is invalid") from exc
                    if expires <= datetime.now(UTC):
                        raise AuthorizationError("token expired")
                return principal
        if self.oidc:
            try:
                return verify_oidc_token(supplied, self.oidc)
            except IdentityError as exc:
                raise AuthorizationError(str(exc)) from exc
        raise AuthorizationError("invalid bearer token")

    def _rate_allowed(self, authorization: str) -> bool:
        if self.rate_limit <= 0:
            return True
        key = hashlib.sha256(authorization.encode()).hexdigest()
        now = time.monotonic()
        window = self._requests.setdefault(key, deque())
        cutoff = now - self.rate_window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.rate_limit:
            return False
        window.append(now)
        return True

    @staticmethod
    def _authorize(principal: dict[str, Any], role: str, bucket: str | None = None) -> None:
        roles = {str(item) for item in principal.get("roles", [])}
        if "admin" not in roles and role not in roles:
            raise AuthorizationError(f"role {role} is required")
        buckets = principal.get("buckets")
        if bucket and "admin" not in roles and isinstance(buckets, list) and bucket not in buckets:
            raise AuthorizationError(f"access to bucket {bucket} is denied")

    @staticmethod
    def _is_admin(principal: dict[str, Any]) -> bool:
        return "admin" in {str(item) for item in principal.get("roles", [])}

    @classmethod
    def _visible(cls, principal: dict[str, Any], bucket: object) -> bool:
        if cls._is_admin(principal):
            return True
        allowed = principal.get("buckets")
        return isinstance(allowed, list) and str(bucket or "") in {
            str(item) for item in allowed}

    @staticmethod
    def _page(query: dict[str, list[str]]) -> tuple[int, int]:
        try:
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError as exc:
            raise ValueError("limit and offset must be integers") from exc
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        return limit, offset

    @staticmethod
    def _summary_run(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in (
            "run_id", "created_at", "status", "state", "mode", "profile",
            "evidence_count")}

    def _audit(self, principal: dict[str, Any], action: str, resource: str,
               outcome: str) -> None:
        path = self.root / "audit" / "service.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = _state_lock(path)
        try:
            event = {"timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "subject": principal.get("subject", "unknown"), "action": action,
                     "resource": resource, "outcome": outcome}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        finally:
            lock.rmdir()

    def dispatch(self, method: str, raw_path: str, authorization: str, *,
                 body: bytes = b"", headers: dict[str, str] | None = None
                 ) -> tuple[int, str, bytes]:
        incoming = (headers or {}).get("Traceparent", (headers or {}).get("traceparent", ""))
        trace_id, parent_span = parse_traceparent(incoming)
        recorder = TraceRecorder(self.root)
        with recorder.span("http.request", attributes={"http.method": method,
                "http.route": urlparse(raw_path).path}, trace_id=trace_id,
                parent_span_id=parent_span) as span:
            response = self._dispatch(method, raw_path, authorization, body=body, headers=headers)
            span["attributes"]["http.status_code"] = response[0]
            return response

    def _dispatch(self, method: str, raw_path: str, authorization: str, *,
                  body: bytes = b"", headers: dict[str, str] | None = None
                  ) -> tuple[int, str, bytes]:
        try:
            parsed = urlparse(raw_path)
            if method == "GET" and parsed.path in {"/", "/console"}:
                return 200, "text/html; charset=utf-8", CONSOLE_HTML
            if method == "GET" and parsed.path == "/console.css":
                return 200, "text/css; charset=utf-8", CONSOLE_CSS
            if method == "GET" and parsed.path == "/console.js":
                return 200, "text/javascript; charset=utf-8", CONSOLE_JS
            if method == "GET" and parsed.path in {"/healthz", "/api/v1/health"}:
                return self._json(200, {"status": "ok", "service": "ohbs-image"})
            if method == "GET" and parsed.path in {"/readyz", "/api/v1/ready"}:
                self._load_rbac()
                ready = self.root.exists() and self.root.is_dir()
                return self._json(200 if ready else 503, {
                    "status": "ready" if ready else "not_ready",
                    "service": "ohbs-image"})
            if not self._rate_allowed(authorization):
                return self._error(429, "rate_limited", "request rate limit exceeded")
            principal = self.principal(authorization)
            parts = [part for part in parsed.path.split("/") if part]
            if parts[:2] != ["api", "v1"]:
                return self._error(404, "not_found", "route not found")
            route = parts[2:]
            if method == "POST" and route == ["approvals"]:
                self._authorize(principal, "promoter")
                payload = json.loads(body.decode("utf-8"))
                resource = str(payload.get("resource") or "")
                operation = payload.get("payload")
                if not resource or not isinstance(operation, dict):
                    raise ValueError("approval resource and payload are required")
                result = create_approval(
                    self.root, requester=str(principal.get("subject") or "unknown"),
                    action=str(payload.get("action") or "channel.promote"), resource=resource,
                    payload=operation,
                    required=int(self.approval_policy.get("minimum_approvals", 2)),
                    ttl_seconds=int(self.approval_policy.get("ttl_seconds", 3600)))
                self._audit(principal, "approval.create", result["approval_id"], "allowed")
                return self._json(201, result)
            if method == "POST" and len(route) == 3 and route[0] == "approvals" \
                    and route[2] == "approve":
                self._authorize(principal, "approver")
                result = approve(self.root, route[1],
                                 approver=str(principal.get("subject") or "unknown"))
                self._audit(principal, "approval.approve", route[1], "allowed")
                return self._json(200, result)
            if method == "GET" and route == ["approvals"]:
                self._authorize(principal, "approver")
                query = parse_qs(parsed.query)
                approval_rows = []
                for path in sorted((self.root / "approvals").glob("*.json")):
                    item = _read_object(path)
                    if item is not None and item.get("document_hash") == _hash(item):
                        approval_rows.append(item)
                if query.get("status"):
                    approval_rows = [row for row in approval_rows
                                     if row.get("status") == query["status"][0]]
                approval_rows.sort(
                    key=lambda row: str(row.get("created_at") or ""), reverse=True)
                limit, offset = self._page(query)
                return self._json(200, {"count": len(approval_rows), "limit": limit,
                    "offset": offset, "approvals": approval_rows[offset:offset + limit]})
            if method == "GET" and route == ["audit"]:
                self._authorize(principal, "auditor")
                query = parse_qs(parsed.query)
                audit_rows: list[dict[str, Any]] = []
                audit_path = self.root / "audit" / "service.jsonl"
                if audit_path.is_file():
                    for line in audit_path.read_text(encoding="utf-8").splitlines():
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict):
                            audit_rows.append(item)
                for field in ("subject", "action", "outcome"):
                    if query.get(field):
                        audit_rows = [row for row in audit_rows
                                      if row.get(field) == query[field][0]]
                limit, offset = self._page(query)
                audit_rows.reverse()
                return self._json(200, {"count": len(audit_rows), "limit": limit,
                    "offset": offset, "events": audit_rows[offset:offset + limit]})
            if method == "GET" and route == ["artifacts"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                limit, offset = self._page(query)
                requested_bucket = query.get("bucket", [""])[0]
                if requested_bucket and not self._visible(principal, requested_bucket):
                    raise AuthorizationError("access to artifact bucket is denied")
                count, rows = _database(self.root / "registry").search_artifacts(
                    bucket=requested_bucket, status=query.get("status", [""])[0],
                    version=query.get("version", [""])[0],
                    query=query.get("q", [""])[0], label=query.get("label", [""])[0],
                    limit=limit, offset=offset)
                rows = [row for row in rows if self._visible(
                    principal, row.get("bucket"))]
                visible_count = count if self._is_admin(principal) or requested_bucket else len(rows)
                return self._json(200, {"count": visible_count, "limit": limit,
                    "offset": offset, "artifacts": rows})
            if method == "POST" and route == ["artifacts"]:
                self._authorize(principal, "admin")
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("artifact payload must be an object")
                put_artifact(payload, self.root / "registry")
                artifact_id = str(payload.get("artifact_id") or "")
                self._audit(principal, "artifact.write", artifact_id, "allowed")
                return self._json(201, payload)
            if method == "GET" and len(route) == 2 and route[0] == "artifacts":
                doc = get_artifact(route[1], self.root / "registry")
                if doc is None:
                    return self._error(404, "not_found", "artifact not found")
                self._authorize(principal, "viewer", str(doc.get("bucket") or ""))
                return self._json(200, doc)
            if method == "PATCH" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "status":
                self._authorize(principal, "admin")
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("status payload must be an object")
                result = change_artifact_status(
                    route[1], str(payload.get("status") or ""),
                    actor=str(principal.get("subject") or "unknown"),
                    reason=str(payload.get("reason") or ""),
                    auto_rollback=bool(payload.get("auto_rollback", True)),
                    root=self.root / "registry")
                self._audit(principal, "artifact.status", route[1], "allowed")
                return self._json(200, result)
            if method == "POST" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "rebuild":
                self._authorize(principal, "admin")
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("rebuild payload must be an object")
                operation_id = (headers or {}).get("Idempotency-Key", "")
                if not operation_id:
                    return self._error(400, "idempotency_required",
                                       "Idempotency-Key is required")
                event = {"schema": EVENT_SCHEMA, "event_id": operation_id,
                    "type": "base_image.updated", "artifact_id": route[1],
                    "occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reason": str(payload.get("reason") or "operator requested rebuild")}
                result = process_rebuild_event(
                    event, apply=True, actor=str(principal.get("subject") or "unknown"),
                    root=self.root / "registry")
                self._audit(principal, "artifact.rebuild", route[1], "allowed")
                return self._json(202, result)
            if method == "POST" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "distribute":
                self._authorize(principal, "admin")
                if (headers or {}).get("X-Confirm-Cost", "").lower() != "true":
                    return self._error(400, "cost_confirmation_required",
                                       "X-Confirm-Cost: true is required")
                payload = json.loads(body.decode("utf-8"))
                regions = payload.get("regions") if isinstance(payload, dict) else None
                if not isinstance(regions, list) or not all(
                        isinstance(region, str) and region for region in regions):
                    raise ValueError("distribution requires a regions array")
                result = execute_distribution(route[1], regions, apply=True,
                                              root=self.root / "registry")
                self._audit(principal, "artifact.distribute", route[1], "allowed")
                return self._json(202, result)
            if method == "GET" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "descendants":
                doc = get_artifact(route[1], self.root / "registry")
                if doc is None:
                    return self._error(404, "not_found", "artifact not found")
                self._authorize(principal, "viewer", str(doc.get("bucket") or ""))
                rows = [item for item in descendants(route[1], self.root / "registry")
                        if self._visible(principal, item.get("bucket"))]
                return self._json(200, {"artifact_id": route[1],
                    "count": len(rows), "descendants": rows})
            if method == "GET" and route == ["runs"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                rows = [row for row in collect_runs(self.root) if self._visible(
                    principal, row.get("profile"))]
                for field in ("profile", "status"):
                    if query.get(field):
                        rows = [row for row in rows
                                if row.get(field) == query[field][0]]
                limit, offset = self._page(query)
                summaries = [self._summary_run(row)
                             for row in rows[offset:offset + limit]]
                return self._json(200, {"count": len(rows), "limit": limit,
                    "offset": offset, "runs": summaries})
            if method == "GET" and len(route) == 2 and route[0] == "runs":
                self._authorize(principal, "viewer")
                row = next((item for item in collect_runs(self.root)
                            if item.get("run_id") == route[1]), None)
                if row is None:
                    return self._error(404, "not_found", "run not found")
                if not self._visible(principal, row.get("profile")):
                    raise AuthorizationError("access to run is denied")
                return self._json(200, {"run": row})
            if method == "GET" and len(route) == 3 and route[0] == "runs" \
                    and route[2] == "evidence":
                self._authorize(principal, "viewer")
                row = next((item for item in collect_runs(self.root)
                            if item.get("run_id") == route[1]), None)
                if row is None:
                    return self._error(404, "not_found", "run not found")
                if not self._visible(principal, row.get("profile")):
                    raise AuthorizationError("access to run is denied")
                query = parse_qs(parsed.query)
                relative = query.get("path", [""])[0]
                allowed = {str(item.get("path")) for item in row.get("evidence", [])
                           if isinstance(item, dict)}
                if relative not in allowed:
                    raise AuthorizationError("evidence path is not attached to this run")
                target = (self.root / relative).resolve()
                if self.root.resolve() not in target.parents or not target.is_file():
                    raise ValueError("evidence path is invalid")
                data = target.read_bytes()
                if len(data) > 262_144:
                    raise ValueError("evidence exceeds the 256 KiB console preview limit")
                try:
                    value = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return 200, "text/plain; charset=utf-8", data
                return self._json(200, value)
            if method == "GET" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "impact":
                doc = get_artifact(route[1], self.root / "registry")
                if doc is None:
                    return self._error(404, "not_found", "artifact not found")
                self._authorize(principal, "viewer", str(doc.get("bucket") or ""))
                result = impact_plan(route[1], self.root / "registry")
                if not self._is_admin(principal):
                    result["artifacts"] = [item for item in result["artifacts"]
                        if self._visible(principal, item.get("bucket"))]
                    result["channels"] = [item for item in result["channels"]
                        if self._visible(principal, item.get("bucket"))]
                    result["affected_count"] = len(result["artifacts"])
                    result["descendant_count"] = max(0, len(result["artifacts"]) - 1)
                    result["channel_count"] = len(result["channels"])
                    result["document_hash"] = _hash(result)
                return self._json(200, result)
            if method == "GET" and route == ["rebuild-requests"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                rebuild_rows: list[dict[str, Any]] = []
                for path in sorted((self.root / "registry" / "rebuild_requests").glob("*.json")):
                    request = _read_object(path)
                    if request is None:
                        continue
                    artifact = get_artifact(
                        str(request.get("artifact_id") or ""), self.root / "registry")
                    if artifact is not None and self._visible(principal, artifact.get("bucket")):
                        rebuild_rows.append(request)
                if query.get("status"):
                    rebuild_rows = [row for row in rebuild_rows
                                    if row.get("status") == query["status"][0]]
                rebuild_rows.sort(
                    key=lambda row: str(row.get("created_at") or ""), reverse=True)
                limit, offset = self._page(query)
                return self._json(200, {"count": len(rebuild_rows), "limit": limit,
                    "offset": offset,
                    "rebuild_requests": rebuild_rows[offset:offset + limit]})
            if method == "GET" and route == ["policies"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                rows = list_policies(self.root / "policy_registry")
                if query.get("status"):
                    rows = [row for row in rows if row.get("status") == query["status"][0]]
                limit, offset = self._page(query)
                return self._json(200, {"count": len(rows), "limit": limit,
                    "offset": offset, "policies": rows[offset:offset + limit]})
            if method == "POST" and route == ["policies", "simulate"]:
                self._authorize(principal, "viewer")
                payload = json.loads(body.decode("utf-8"))
                candidate = payload.get("bundle") if isinstance(payload, dict) else None
                environment = str(payload.get("environment") or "production") \
                    if isinstance(payload, dict) else "production"
                if not isinstance(candidate, dict):
                    raise ValueError("simulation bundle must be an object")
                _count, artifacts = _database(self.root / "registry").search_artifacts(limit=1000)
                artifacts = [artifact for artifact in artifacts
                             if self._visible(principal, artifact.get("bucket"))]
                baseline = None
                try:
                    record = resolve_policy(str(candidate.get("policy_id") or ""),
                                            root=self.root / "policy_registry")
                    value = record.get("bundle")
                    baseline = value if isinstance(value, dict) else None
                except ValueError:
                    pass
                result = simulate_policy(candidate, artifacts, environment, baseline=baseline)
                self._audit(principal, "policy.simulate",
                            str(candidate.get("policy_id") or "candidate"), "allowed")
                return self._json(200, result)
            if method == "GET" and route == ["traces"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                trace_rows: list[dict[str, Any]] = []
                path = self.root / "telemetry" / "traces.jsonl"
                if path.is_file():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict):
                            trace_rows.append(item)
                for field in ("trace_id", "status", "name"):
                    if query.get(field):
                        trace_rows = [row for row in trace_rows
                                      if row.get(field) == query[field][0]]
                trace_rows.reverse()
                limit, offset = self._page(query)
                return self._json(200, {"count": len(trace_rows), "limit": limit,
                    "offset": offset, "spans": trace_rows[offset:offset + limit]})
            if len(route) == 3 and route[0] == "channels":
                bucket, channel = route[1], route[2]
                if method == "GET":
                    self._authorize(principal, "viewer", bucket)
                    return self._json(200, resolve_channel(bucket, channel, self.root / "registry"))
                if method == "PUT":
                    self._authorize(principal, "promoter", bucket)
                    payload = json.loads(body.decode("utf-8"))
                    operation_id = (headers or {}).get("Idempotency-Key", "")
                    if not operation_id:
                        return self._json(400, {"error": "Idempotency-Key is required"})
                    required_channels = {
                        str(item) for item in self.approval_policy.get("required_channels", [])}
                    if channel in required_channels:
                        approval_id = (headers or {}).get("Approval-Id", "")
                        if not approval_id:
                            raise AuthorizationError("Approval-Id is required for this channel")
                        consume_approval(self.root, approval_id, action="channel.promote",
                                         resource=f"{bucket}/{channel}", payload=payload)
                    result = promote_channel(bucket, channel, str(payload.get("artifact_id") or ""),
                        expected_generation=payload.get("expected_generation"),
                        operation_id=operation_id, actor=str(principal.get("subject") or "unknown"),
                        root=self.root / "registry")
                    self._audit(principal, "channel.promote", f"{bucket}/{channel}", "allowed")
                    return self._json(200, result)
            if method == "GET" and route == ["metrics"]:
                self._authorize(principal, "viewer")
                return 200, "text/plain; version=0.0.4", prometheus_metrics(
                    collect_metrics(self.root)).encode()
            return self._error(404, "not_found", "route not found")
        except AuthorizationError as exc:
            return self._error(403, "forbidden", str(exc))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return self._error(400, "invalid_request", str(exc))

    @staticmethod
    def _json(status: int, value: Any) -> tuple[int, str, bytes]:
        return status, "application/json", (json.dumps(value, ensure_ascii=False) + "\n").encode()

    @classmethod
    def _error(cls, status: int, code: str, message: str) -> tuple[int, str, bytes]:
        return cls._json(status, {"error": {"code": code, "message": message}})


def serve_control_plane(host: str, port: int, root: Path, rbac_path: Path, *,
                        max_body_bytes: int = 1_048_576, request_timeout: int = 30,
                        rate_limit: int = 120, rate_window_seconds: int = 60) -> None:
    control = ControlPlane(root, rbac_path, rate_limit=rate_limit,
                           rate_window_seconds=rate_window_seconds)

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            started = time.monotonic()
            self.connection.settimeout(request_timeout)
            length = int(self.headers.get("Content-Length", "0"))
            if length > max_body_bytes:
                status, content_type, response = control._error(
                    413, "payload_too_large", "request body exceeds configured limit")
                self._respond(status, content_type, response, started)
                return
            body = self.rfile.read(length) if length else b""
            status, content_type, response = control.dispatch(
                self.command, self.path, self.headers.get("Authorization", ""), body=body,
                headers=dict(self.headers.items()))
            self._respond(status, content_type, response, started)

        def _respond(self, status: int, content_type: str, response: bytes,
                     started: float) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)
            access = {"timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "method": self.command, "path": self.path.split("?", 1)[0],
                      "status": status,
                      "duration_ms": round((time.monotonic() - started) * 1000, 3),
                      "remote": self.client_address[0]}
            info(json.dumps(access, separators=(",", ":")))

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.serve_forever()


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_remote:
        fail("remote listen requires --allow-remote and a trusted reverse proxy/TLS boundary")
        return 2
    root = _lineage_path().parent
    try:
        info(f"Control plane listening on http://{args.host}:{args.port}/api/v1")
        serve_control_plane(args.host, args.port, root, Path(args.rbac),
                            max_body_bytes=args.max_body_bytes,
                            request_timeout=args.request_timeout,
                            rate_limit=args.rate_limit,
                            rate_window_seconds=args.rate_window_seconds)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    return 0
