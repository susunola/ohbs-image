from __future__ import annotations

import argparse
import hmac
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ._channels import promote_channel, resolve_channel
from ._config import _lineage_path
from ._console import CONSOLE_CSS, CONSOLE_HTML, CONSOLE_JS
from ._logging import fail, info
from ._metrics import collect_metrics, prometheus_metrics
from ._registry import _artifact_path, _read_object, collect_artifacts
from ._reports import _state_lock


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
            principal = self.principal(authorization)
            parts = [part for part in parsed.path.split("/") if part]
            if parts[:2] != ["api", "v1"]:
                return 404, "application/json", b'{"error":"not found"}'
            route = parts[2:]
            if method == "GET" and route == ["artifacts"]:
                self._authorize(principal, "viewer")
                query = parse_qs(parsed.query)
                rows = collect_artifacts(self.root / "registry")
                allowed = principal.get("buckets")
                if isinstance(allowed, list) and "admin" not in principal.get("roles", []):
                    rows = [row for row in rows if row.get("bucket") in allowed]
                if query.get("bucket"):
                    rows = [row for row in rows if row.get("bucket") == query["bucket"][0]]
                return self._json(200, {"count": len(rows), "artifacts": rows})
            if method == "GET" and len(route) == 2 and route[0] == "artifacts":
                doc = _read_object(_artifact_path(route[1], self.root / "registry"))
                if doc is None:
                    return self._json(404, {"error": "artifact not found"})
                self._authorize(principal, "viewer", str(doc.get("bucket") or ""))
                return self._json(200, doc)
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
            return self._json(404, {"error": "not found"})
        except AuthorizationError as exc:
            return self._json(403, {"error": str(exc)})
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})

    @staticmethod
    def _json(status: int, value: Any) -> tuple[int, str, bytes]:
        return status, "application/json", (json.dumps(value, ensure_ascii=False) + "\n").encode()


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
