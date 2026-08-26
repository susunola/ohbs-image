from __future__ import annotations

import argparse
import hmac
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ._ancestry import descendants, impact_plan
from ._channels import promote_channel, resolve_channel
from ._config import _lineage_path
from ._console import CONSOLE_CSS, CONSOLE_HTML, CONSOLE_JS
from ._logging import fail, info
from ._metrics import collect_metrics, prometheus_metrics
from ._registry import _artifact_path, _hash, _read_object, collect_artifacts
from ._reports import _state_lock
from ._runs import collect_runs


class AuthorizationError(ValueError):
    pass


class ControlPlane:
    def __init__(self, root: Path, rbac_path: Path) -> None:
        self.root = root
        try:
            rbac = json.loads(rbac_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid RBAC configuration {rbac_path}") from exc
        tokens = rbac.get("tokens") if isinstance(rbac, dict) else None
        if not isinstance(tokens, dict) or not tokens:
            raise ValueError("RBAC configuration requires a non-empty tokens object")
        self.tokens: dict[str, dict[str, Any]] = {
            str(token): principal for token, principal in tokens.items()
            if isinstance(principal, dict)}

    def principal(self, authorization: str) -> dict[str, Any]:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise AuthorizationError("missing bearer token")
        supplied = authorization[len(prefix):]
        for token, principal in self.tokens.items():
            if hmac.compare_digest(supplied, token):
                return principal
        raise AuthorizationError("invalid bearer token")

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
            principal = self.principal(authorization)
            parts = [part for part in parsed.path.split("/") if part]
            if parts[:2] != ["api", "v1"]:
                return self._error(404, "not_found", "route not found")
            route = parts[2:]
            if method == "GET" and route == ["artifacts"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                rows = collect_artifacts(self.root / "registry")
                rows = [row for row in rows if self._visible(
                    principal, row.get("bucket"))]
                if query.get("bucket"):
                    rows = [row for row in rows if row.get("bucket") == query["bucket"][0]]
                if query.get("status"):
                    rows = [row for row in rows if row.get("status") == query["status"][0]]
                limit, offset = self._page(query)
                return self._json(200, {"count": len(rows), "limit": limit,
                    "offset": offset, "artifacts": rows[offset:offset + limit]})
            if method == "GET" and len(route) == 2 and route[0] == "artifacts":
                doc = _read_object(_artifact_path(route[1], self.root / "registry"))
                if doc is None:
                    return self._error(404, "not_found", "artifact not found")
                self._authorize(principal, "viewer", str(doc.get("bucket") or ""))
                return self._json(200, doc)
            if method == "GET" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "descendants":
                doc = _read_object(_artifact_path(route[1], self.root / "registry"))
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
            if method == "GET" and len(route) == 3 and route[0] == "artifacts" \
                    and route[2] == "impact":
                doc = _read_object(_artifact_path(route[1], self.root / "registry"))
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
                rows: list[dict[str, Any]] = []
                for path in sorted((self.root / "registry" / "rebuild_requests").glob("*.json")):
                    request = _read_object(path)
                    if request is None:
                        continue
                    artifact = _read_object(_artifact_path(
                        str(request.get("artifact_id") or ""), self.root / "registry"))
                    if artifact is not None and self._visible(principal, artifact.get("bucket")):
                        rows.append(request)
                if query.get("status"):
                    rows = [row for row in rows if row.get("status") == query["status"][0]]
                rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
                limit, offset = self._page(query)
                return self._json(200, {"count": len(rows), "limit": limit,
                    "offset": offset, "rebuild_requests": rows[offset:offset + limit]})
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


def serve_control_plane(host: str, port: int, root: Path, rbac_path: Path) -> None:
    control = ControlPlane(root, rbac_path)

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            status, content_type, response = control.dispatch(
                self.command, self.path, self.headers.get("Authorization", ""), body=body,
                headers=dict(self.headers.items()))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_remote:
        fail("remote listen requires --allow-remote and a trusted reverse proxy/TLS boundary")
        return 2
    root = _lineage_path().parent
    try:
        info(f"Control plane listening on http://{args.host}:{args.port}/api/v1")
        serve_control_plane(args.host, args.port, root, Path(args.rbac))
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    return 0
