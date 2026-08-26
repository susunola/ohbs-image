from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._registry import _hash, _read_object, _root
from ._reports import _atomic_write_bytes, _state_lock

OPERATION_SCHEMA = "https://ohbs-image.dev/operation/v1"
FENCE_SCHEMA = "https://ohbs-image.dev/fence/v1"
_SAFE_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}")


class StaleFencingTokenError(ValueError):
    """A worker attempted to commit after a newer worker took ownership."""


def _scope_key(scope: str) -> str:
    if not _SAFE_SCOPE.fullmatch(scope) or ".." in scope:
        raise ValueError(f"invalid operation scope {scope!r}")
    digest = hashlib.sha256(scope.encode()).hexdigest()[:20]
    return f"{digest}-{scope.replace('/', '_').replace(':', '_')}"


def _operation_key(operation_id: str) -> str:
    if not operation_id.strip() or len(operation_id) > 256:
        raise ValueError("operation_id must contain 1-256 characters")
    return hashlib.sha256(operation_id.encode()).hexdigest()


def _paths(root: Path | None, scope: str, operation_id: str) -> tuple[Path, Path]:
    base = _root(root) / "operations" / _scope_key(scope)
    return base / "fence.json", base / f"{_operation_key(operation_id)}.json"


def _write(path: Path, doc: dict[str, Any]) -> None:
    doc["document_hash"] = _hash(doc)
    _atomic_write_bytes(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())


def verify_fencing_token(scope: str, token: int, root: Path | None = None) -> None:
    fence, _operation = _paths(root, scope, "verify")
    current = _read_object(fence)
    if current is None or current.get("document_hash") != _hash(current):
        raise StaleFencingTokenError(f"fence for {scope} is missing or invalid")
    latest = int(current.get("token", 0))
    if token != latest:
        raise StaleFencingTokenError(
            f"stale fencing token {token} for {scope}; latest token is {latest}")


@contextmanager
def fenced_operation(scope: str, operation_id: str, *,
                     root: Path | None = None) -> Iterator[dict[str, Any]]:
    """Claim an operation and retain a monotonically increasing fencing token.

    State must live on storage shared by every worker. A completed operation is
    replayed without executing its mutation again. Callers put their response in
    ``claim["result"]`` before leaving the context.
    """
    fence_path, operation_path = _paths(root, scope, operation_id)
    fence_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(fence_path)
    claim: dict[str, Any]
    try:
        existing = _read_object(operation_path)
        if (existing is not None and existing.get("document_hash") == _hash(existing)
                and existing.get("status") == "completed"):
            yield {"replay": True, "token": int(existing["fencing_token"]),
                   "result": existing.get("result")}
            return
        fence = _read_object(fence_path)
        token = int(fence.get("token", 0)) + 1 if fence else 1
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        fence_doc: dict[str, Any] = {
            "schema": FENCE_SCHEMA, "scope": scope, "token": token,
            "operation_id": operation_id, "claimed_at": now,
        }
        _write(fence_path, fence_doc)
        operation_doc: dict[str, Any] = {
            "schema": OPERATION_SCHEMA, "scope": scope,
            "operation_id": operation_id, "fencing_token": token,
            "status": "in_progress", "started_at": now,
        }
        _write(operation_path, operation_doc)
        claim = {"replay": False, "token": token, "result": None}
        yield claim
        verify_fencing_token(scope, token, root)
        operation_doc.update(
            status="completed", completed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            result=claim.get("result"))
        _write(operation_path, operation_doc)
    finally:
        lock.rmdir()
