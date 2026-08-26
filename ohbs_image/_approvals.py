from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._registry import _hash, _read_object
from ._reports import _atomic_write_bytes, _state_lock

APPROVAL_SCHEMA = "https://ohbs-image.dev/approval-request/v1"
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(root: Path, approval_id: str) -> Path:
    if not _SAFE.fullmatch(approval_id):
        raise ValueError("approval id is unsafe")
    return root / "approvals" / f"{approval_id}.json"


def create_approval(root: Path, *, requester: str, action: str, resource: str,
                    payload: dict[str, Any], required: int = 2,
                    ttl_seconds: int = 3600) -> dict[str, Any]:
    if required < 1 or ttl_seconds < 60:
        raise ValueError("approval policy is invalid")
    approval_id = f"apr-{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    document: dict[str, Any] = {
        "schema": APPROVAL_SCHEMA, "approval_id": approval_id, "status": "pending",
        "requester": requester, "action": action, "resource": resource,
        "payload": payload, "required_approvals": required, "approvals": [],
        "created_at": _stamp(now), "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)),
    }
    document["document_hash"] = _hash(document)
    path = _path(root, approval_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write_bytes(path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
    return document


def approve(root: Path, approval_id: str, *, approver: str) -> dict[str, Any]:
    path = _path(root, approval_id)
    lock = _state_lock(path)
    try:
        document = _read_object(path)
        if document is None or document.get("document_hash") != _hash(document):
            raise ValueError("approval request not found or corrupt")
        if document.get("status") not in {"pending", "approved"}:
            raise ValueError(f"approval request is {document.get('status')}")
        expires = datetime.fromisoformat(str(document["expires_at"]).replace("Z", "+00:00"))
        if expires <= datetime.now(UTC):
            document["status"] = "expired"
            raise ValueError("approval request expired")
        if approver == document.get("requester"):
            raise ValueError("requester cannot approve their own operation")
        approvals = list(document.get("approvals") or [])
        if any(item.get("subject") == approver for item in approvals):
            return document
        approvals.append({"subject": approver, "approved_at": _stamp()})
        document["approvals"] = approvals
        if len(approvals) >= int(document["required_approvals"]):
            document["status"] = "approved"
        document["document_hash"] = _hash(document)
        _atomic_write_bytes(path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
        return document
    finally:
        lock.rmdir()


def consume_approval(root: Path, approval_id: str, *, action: str, resource: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
    path = _path(root, approval_id)
    lock = _state_lock(path)
    try:
        document = _read_object(path)
        if document is None or document.get("document_hash") != _hash(document):
            raise ValueError("approval request not found or corrupt")
        if document.get("status") != "approved":
            raise ValueError("approval request has not reached quorum")
        if document.get("action") != action or document.get("resource") != resource \
                or document.get("payload") != payload:
            raise ValueError("approval request does not match this operation")
        expires = datetime.fromisoformat(str(document["expires_at"]).replace("Z", "+00:00"))
        if expires <= datetime.now(UTC):
            raise ValueError("approval request expired")
        document.update(status="consumed", consumed_at=_stamp())
        document["document_hash"] = _hash(document)
        _atomic_write_bytes(path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
        return document
    finally:
        lock.rmdir()
