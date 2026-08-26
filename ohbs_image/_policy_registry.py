from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._config import _state_dir
from ._logging import fail, ok
from ._policy import _enforce_policy_trust, load_policy, verify_policy
from ._registry import _hash, _read_object
from ._reports import _atomic_write_bytes, _state_lock

POLICY_RECORD_SCHEMA = "https://ohbs-image.dev/policy-registry-record/v1"
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root(root: Path | None = None) -> Path:
    return (root or (_state_dir() / "policy_registry")).expanduser().resolve()


def _record_path(policy_id: str, version: str, root: Path | None = None) -> Path:
    if not _SAFE.fullmatch(policy_id) or not _SAFE.fullmatch(version):
        raise ValueError("policy id or version is unsafe")
    return _root(root) / "policies" / policy_id / f"{version}.json"


def publish_policy(bundle: Path, *, actor: str, activate: bool = False,
                   root: Path | None = None) -> dict[str, Any]:
    document = load_policy(bundle)
    failures = verify_policy(document)
    if failures:
        raise ValueError("invalid policy: " + "; ".join(failures))
    signer = _enforce_policy_trust(bundle.resolve(), document)
    policy_id, version = str(document["policy_id"]), str(document.get("version") or "")
    if not _SAFE.fullmatch(version):
        raise ValueError("policy version is missing or unsafe")
    path = _record_path(policy_id, version, root)
    registry = _root(root)
    registry.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(registry / ".policy-registry")
    try:
        existing = _read_object(path)
        bundle_hash = _hash(document)
        if existing is not None:
            if existing.get("bundle_hash") != bundle_hash:
                raise ValueError(f"immutable policy version already exists: {policy_id}@{version}")
            record = existing
            if not activate:
                return record
            record["status"] = "active"
            record["document_hash"] = _hash(record)
        else:
            record = {
                "schema": POLICY_RECORD_SCHEMA, "policy_id": policy_id, "version": version,
                "status": "active" if activate else "published", "bundle": document,
                "bundle_hash": bundle_hash, "published_at": _stamp(), "published_by": actor,
            }
            if signer:
                record["signer_fingerprint"] = signer
            record["document_hash"] = _hash(record)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write_bytes(path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
        if activate:
            pointer = {"policy_id": policy_id, "version": version, "activated_at": _stamp(),
                       "activated_by": actor}
            pointer["document_hash"] = _hash(pointer)
            active = registry / "active" / f"{policy_id}.json"
            active.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write_bytes(active, (json.dumps(pointer, indent=2) + "\n").encode())
        return record
    finally:
        lock.rmdir()


def revoke_policy(policy_id: str, version: str, *, actor: str, reason: str,
                  root: Path | None = None) -> dict[str, Any]:
    registry = _root(root)
    lock = _state_lock(registry / ".policy-registry")
    try:
        path = _record_path(policy_id, version, root)
        record = _read_object(path)
        if record is None or record.get("document_hash") != _hash(record):
            raise ValueError(f"policy not found or corrupt: {policy_id}@{version}")
        record.update(status="revoked", revoked_at=_stamp(), revoked_by=actor,
                      revocation_reason=reason)
        record["document_hash"] = _hash(record)
        _atomic_write_bytes(path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode())
        active = registry / "active" / f"{policy_id}.json"
        pointer = _read_object(active)
        if pointer is not None and pointer.get("version") == version:
            active.unlink()
        return record
    finally:
        lock.rmdir()


def resolve_policy(policy_id: str, version: str = "", *,
                   root: Path | None = None) -> dict[str, Any]:
    if not version:
        pointer = _read_object(_root(root) / "active" / f"{policy_id}.json")
        if pointer is None or pointer.get("document_hash") != _hash(pointer):
            raise ValueError(f"no active policy: {policy_id}")
        version = str(pointer.get("version") or "")
    record = _read_object(_record_path(policy_id, version, root))
    if record is None or record.get("document_hash") != _hash(record):
        raise ValueError(f"policy not found or corrupt: {policy_id}@{version}")
    if record.get("status") == "revoked":
        raise ValueError(f"policy is revoked: {policy_id}@{version}")
    return record


def list_policies(root: Path | None = None) -> list[dict[str, Any]]:
    records = []
    for path in sorted((_root(root) / "policies").glob("*/*.json")):
        record = _read_object(path)
        if record is not None and record.get("document_hash") == _hash(record):
            records.append(record)
    return records


def cmd_policy_publish(args: argparse.Namespace) -> int:
    try:
        record = publish_policy(Path(args.bundle), actor=args.actor, activate=args.activate)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    ok(f"Published {record['policy_id']}@{record['version']} ({record['status']})")
    return 0


def cmd_policy_resolve(args: argparse.Namespace) -> int:
    try:
        record = resolve_policy(args.policy_id, args.version)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_policy_list(args: argparse.Namespace) -> int:
    print(json.dumps(list_policies(), ensure_ascii=False, indent=2))
    return 0


def cmd_policy_revoke(args: argparse.Namespace) -> int:
    try:
        record = revoke_policy(args.policy_id, args.version, actor=args.actor, reason=args.reason)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    ok(f"Revoked {record['policy_id']}@{record['version']}")
    return 0
