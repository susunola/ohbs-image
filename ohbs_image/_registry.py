from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail, ok, warn
from ._reports import _atomic_write_bytes, _state_lock

REGISTRY_SCHEMA = "https://ohbs-image.dev/artifact-registry/v1"
ARTIFACT_SCHEMA = "https://ohbs-image.dev/artifact/v1"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _root(root: Path | None = None) -> Path:
    return root or _lineage_path().parent / "registry"


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _hash(doc: dict[str, Any]) -> str:
    payload = {key: value for key, value in doc.items() if key != "document_hash"}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _artifact_path(image_id: str, root: Path | None = None) -> Path:
    if not _SAFE_ID.fullmatch(image_id):
        raise ValueError(f"invalid image ID {image_id!r}")
    return _root(root) / "artifacts" / f"{image_id}.json"


def artifact_from_release(release: dict[str, Any], source: Path) -> dict[str, Any]:
    image_id = str(release.get("image_id") or "")
    if not _SAFE_ID.fullmatch(image_id):
        raise ValueError("release manifest needs a safe image_id")
    profile = str(release.get("profile") or "unknown")
    run_id = str(release.get("run_id") or "")
    doc: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": image_id,
        "bucket": profile,
        "version": run_id or image_id,
        "build_id": run_id,
        "platform": "tencentcloud",
        "region": str(release.get("region") or ""),
        "profile": profile,
        "cis_level": release.get("cis_level"),
        "score": release.get("score"),
        "attestation_signed": bool(release.get("attestation_signed")),
        "status": "active" if release.get("state") != "revoked" else "revoked",
        "created_at": str(release.get("approved_at") or ""),
        "source_release": str(source),
        "evidence": release.get("evidence") if isinstance(release.get("evidence"), dict) else {},
        "labels": {"profile": profile, "region": str(release.get("region") or "")},
    }
    doc["document_hash"] = _hash(doc)
    return doc


def register_release(path: Path, root: Path | None = None) -> Path:
    release = _read_object(path)
    if release is None:
        raise ValueError(f"invalid release manifest {path}")
    doc = artifact_from_release(release, path)
    destination = _artifact_path(str(doc["artifact_id"]), root)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(destination)
    try:
        _atomic_write_bytes(destination,
                            (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())
    finally:
        lock.rmdir()
    return destination


def collect_artifacts(root: Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((_root(root) / "artifacts").glob("*.json")):
        doc = _read_object(path)
        if doc is not None:
            rows.append(doc)
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def rebuild_registry(state: Path, root: Path | None = None) -> dict[str, Any]:
    registered: list[str] = []
    failures: list[dict[str, str]] = []
    for path in sorted((state / "releases").glob("*.json")):
        try:
            registered.append(str(register_release(path, root)))
        except (OSError, ValueError) as exc:
            failures.append({"path": str(path), "error": str(exc)})
    rows = collect_artifacts(root)
    index: dict[str, Any] = {
        "schema": REGISTRY_SCHEMA,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifact_count": len(rows),
        "buckets": sorted({str(row.get("bucket") or "unknown") for row in rows}),
        "artifacts": [{key: row.get(key) for key in (
            "artifact_id", "bucket", "version", "status", "created_at")}
            for row in rows],
    }
    index["document_hash"] = _hash(index)
    registry_root = _root(root)
    registry_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write_bytes(registry_root / "index.json",
                        (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode())
    return {"registered": len(registered), "failed": failures, "index": index}


def verify_registry(root: Path | None = None) -> list[str]:
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for doc in collect_artifacts(root):
        artifact_id = str(doc.get("artifact_id") or "")
        if doc.get("schema") != ARTIFACT_SCHEMA:
            failures.append(f"{artifact_id}: schema mismatch")
        if doc.get("document_hash") != _hash(doc):
            failures.append(f"{artifact_id}: document hash mismatch")
        identity = (str(doc.get("bucket") or ""), str(doc.get("version") or ""))
        if identity in seen:
            failures.append(f"{artifact_id}: duplicate bucket/version {identity[0]}/{identity[1]}")
        seen.add(identity)
    return failures


def change_artifact_status(artifact_id: str, status: str, *, actor: str,
                           reason: str, auto_rollback: bool = True,
                           root: Path | None = None) -> dict[str, Any]:
    if status not in {"quarantined", "revoked"}:
        raise ValueError(f"unsupported artifact status {status!r}")
    if not reason.strip():
        raise ValueError("a non-empty reason is required")
    path = _artifact_path(artifact_id, root)
    lock = _state_lock(path)
    try:
        doc = _read_object(path)
        if doc is None:
            raise ValueError(f"artifact {artifact_id} not found")
        if doc.get("document_hash") != _hash(doc):
            raise ValueError(f"artifact {artifact_id} failed integrity verification")
        previous_status = str(doc.get("status") or "")
        if previous_status == "revoked" and status != "revoked":
            raise ValueError(f"artifact {artifact_id} is permanently revoked")
        doc["status"] = status
        doc["status_changed_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["status_changed_by"] = actor
        doc["status_reason"] = reason.strip()
        history = doc.get("status_history")
        if not isinstance(history, list):
            history = []
        history.append({"from": previous_status, "to": status,
                        "at": doc["status_changed_at"], "actor": actor,
                        "reason": reason.strip()})
        doc["status_history"] = history
        doc["document_hash"] = _hash(doc)
        _atomic_write_bytes(path,
                            (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())
    finally:
        lock.rmdir()

    rollbacks: list[dict[str, Any]] = []
    if auto_rollback:
        from ._channels import rollback_channels_for_artifact
        rollbacks = rollback_channels_for_artifact(
            artifact_id, actor=actor, reason=reason.strip(), root=root)
    return {"artifact": doc, "channel_rollbacks": rollbacks}


def cmd_registry_status(args: argparse.Namespace) -> int:
    try:
        result = change_artifact_status(
            args.artifact_id, args.artifact_status, actor=args.actor,
            reason=args.reason, auto_rollback=not args.no_auto_rollback)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        ok(f"Artifact {args.artifact_id} is now {args.artifact_status}")
        for row in result["channel_rollbacks"]:
            if row["status"] == "rolled_back":
                ok(f"Channel {row['bucket']}/{row['channel']} rolled back to {row['to']}")
            else:
                warn(f"Channel {row['bucket']}/{row['channel']} rollback blocked: "
                     f"{row['error']}")
    return 1 if any(row["status"] == "blocked"
                    for row in result["channel_rollbacks"]) else 0


def cmd_registry_rebuild(args: argparse.Namespace) -> int:
    result = rebuild_registry(_lineage_path().parent)
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        ok(f"Registry rebuilt: {result['registered']} artifact(s)")
        for item in result["failed"]:
            warn(f"Skipped {item['path']}: {item['error']}")
    return 1 if result["failed"] else 0


def cmd_registry_list(args: argparse.Namespace) -> int:
    rows = collect_artifacts()
    if args.bucket:
        rows = [row for row in rows if row.get("bucket") == args.bucket]
    if args.status:
        rows = [row for row in rows if row.get("status") == args.status]
    if args.output == "json":
        print(json.dumps({"schema": REGISTRY_SCHEMA, "count": len(rows),
                          "artifacts": rows}, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(f"{str(row.get('created_at') or '?'):20s}  {str(row.get('status')):8s}  "
              f"{str(row.get('bucket')):14s}  {row.get('artifact_id')}")
    return 0


def cmd_registry_show(args: argparse.Namespace) -> int:
    try:
        doc = _read_object(_artifact_path(args.artifact_id))
    except ValueError as exc:
        fail(str(exc))
        return 2
    if doc is None:
        fail(f"Artifact {args.artifact_id} not found")
        return 1
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        for key in ("artifact_id", "bucket", "version", "status", "profile",
                    "region", "score", "attestation_signed", "created_at"):
            print(f"{key}: {doc.get(key)}")
    return 0


def cmd_registry_verify(args: argparse.Namespace) -> int:
    failures = verify_registry()
    if args.output == "json":
        print(json.dumps({"valid": not failures, "failures": failures}, indent=2))
    elif failures:
        for message in failures:
            fail(message)
    else:
        ok("Artifact registry is valid")
    return 1 if failures else 0
