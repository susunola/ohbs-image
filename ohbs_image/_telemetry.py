from __future__ import annotations

import contextvars
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._reports import _state_lock

TRACE_SCHEMA = "https://ohbs-image.dev/trace-span/v1"
_current_span: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "ohbs_image_span", default=None)


def _hex(length: int) -> str:
    return secrets.token_hex(length // 2)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _otlp_span(span: dict[str, Any]) -> dict[str, Any]:
    attributes = [{"key": key, "value": {"stringValue": str(value)}}
                  for key, value in sorted(span.get("attributes", {}).items())]
    status = {"code": 1 if span["status"] == "ok" else 2,
              "message": str(span.get("error") or "")}
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name",
        "value": {"stringValue": "ohbs-image"}}]}, "scopeSpans": [{"scope": {
        "name": "ohbs-image"}, "spans": [{"traceId": span["trace_id"],
        "spanId": span["span_id"], "parentSpanId": span.get("parent_span_id", ""),
        "name": span["name"], "kind": 1, "startTimeUnixNano": str(span["start_ns"]),
        "endTimeUnixNano": str(span["end_ns"]), "attributes": attributes,
        "status": status}]}]}]}


def push_otlp(payload: dict[str, Any], endpoint: str, *, timeout: int = 10) -> None:
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces") and "resourceSpans" in payload:
        url += "/v1/traces"
    if not url.endswith("/v1/metrics") and "resourceMetrics" in payload:
        url += "/v1/metrics"
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if not 200 <= response.status < 300:
                raise OSError(f"OTLP endpoint returned HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OSError(f"OTLP push failed: {exc}") from exc


class TraceRecorder:
    def __init__(self, root: Path, *, endpoint: str = ""):
        self.root = root.expanduser().resolve()
        self.endpoint = endpoint or os.environ.get("OHBS_IMAGE_OTLP_ENDPOINT", "")

    @contextmanager
    def span(self, name: str, *, attributes: dict[str, Any] | None = None,
             trace_id: str = "", parent_span_id: str = "") -> Iterator[dict[str, Any]]:
        parent = _current_span.get()
        actual_trace = trace_id or (parent[0] if parent else _hex(32))
        actual_parent = parent_span_id or (parent[1] if parent else "")
        span_id = _hex(16)
        start_ns = time.time_ns()
        span: dict[str, Any] = {"schema": TRACE_SCHEMA, "trace_id": actual_trace,
            "span_id": span_id, "parent_span_id": actual_parent, "name": name,
            "start_ns": start_ns, "started_at": _stamp(), "attributes": attributes or {}}
        token = _current_span.set((actual_trace, span_id))
        try:
            yield span
            span.setdefault("status", "ok")
        except Exception as exc:
            span.update(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span["end_ns"] = time.time_ns()
            span["duration_ms"] = round((span["end_ns"] - start_ns) / 1_000_000, 3)
            _current_span.reset(token)
            self._record(span)

    def _record(self, span: dict[str, Any]) -> None:
        path = self.root / "telemetry" / "traces.jsonl"
        with suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = _state_lock(path)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(span, ensure_ascii=False, separators=(",", ":")) + "\n")
            finally:
                lock.rmdir()
        if self.endpoint:
            try:
                push_otlp(_otlp_span(span), self.endpoint)
            except OSError as exc:
                error_path = self.root / "telemetry" / "export-errors.jsonl"
                with suppress(OSError), error_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"timestamp": _stamp(), "error": str(exc),
                                             "trace_id": span["trace_id"]}) + "\n")


def parse_traceparent(value: str) -> tuple[str, str]:
    parts = value.strip().split("-")
    if len(parts) != 4 or parts[0] != "00" or len(parts[1]) != 32 or len(parts[2]) != 16:
        return "", ""
    try:
        int(parts[1], 16)
        int(parts[2], 16)
    except ValueError:
        return "", ""
    if set(parts[1]) == {"0"} or set(parts[2]) == {"0"}:
        return "", ""
    return parts[1].lower(), parts[2].lower()


class TrendStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS metric_snapshots ("
                "recorded_at TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")

    def record(self, snapshot: dict[str, Any], *, recorded_at: str = "") -> str:
        timestamp = recorded_at or datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT OR REPLACE INTO metric_snapshots VALUES(?,?)",
                               (timestamp, json.dumps(snapshot, separators=(",", ":"))))
        return timestamp

    def query(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("trend limit must be between 1 and 1000")
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT recorded_at,snapshot FROM metric_snapshots "
                                      "ORDER BY recorded_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"recorded_at": row[0], "snapshot": json.loads(row[1])} for row in rows]
