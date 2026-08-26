from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._config import ResolvedConfig, _lineage_path
from ._logging import fail
from ._reports import _atomic_write_bytes, _build_fingerprint, _state_lock

CHECKPOINT_SCHEMA = "https://ohbs-image.dev/build-checkpoints/v1"


def _path(run_id: str, root: Path | None = None) -> Path:
    return (root or _lineage_path().parent) / "checkpoints" / f"{run_id}.json"


def read_build_checkpoints(run_id: str, root: Path | None = None) -> dict[str, Any] | None:
    try:
        value = json.loads(_path(run_id, root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _digest(doc: dict[str, Any]) -> str:
    payload = {key: value for key, value in doc.items() if key != "document_hash"}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_build_checkpoint(r: ResolvedConfig, phase: str,
                           artifacts: dict[str, Any] | None = None,
                           root: Path | None = None) -> Path:
    path = _path(r.run_id, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(path)
    try:
        doc = read_build_checkpoints(r.run_id, root) or {
            "schema": CHECKPOINT_SCHEMA, "run_id": r.run_id,
            "build_fingerprint": _build_fingerprint(r), "completed_phases": [],
            "artifacts": {},
        }
        if doc.get("build_fingerprint") != _build_fingerprint(r):
            raise ValueError("build inputs changed since checkpoint creation")
        completed = doc["completed_phases"]
        if phase not in completed:
            completed.append(phase)
        if artifacts:
            doc["artifacts"].update(artifacts)
        doc["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["document_hash"] = _digest(doc)
        _atomic_write_bytes(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())
        return path
    finally:
        lock.rmdir()


def verify_build_checkpoint(run_id: str, root: Path | None = None) -> list[str]:
    doc = read_build_checkpoints(run_id, root)
    if doc is None:
        return ["checkpoint document is missing or invalid"]
    failures: list[str] = []
    if doc.get("schema") != CHECKPOINT_SCHEMA:
        failures.append("checkpoint schema mismatch")
    if doc.get("run_id") != run_id:
        failures.append("checkpoint run_id mismatch")
    if doc.get("document_hash") != _digest(doc):
        failures.append("checkpoint document hash mismatch")
    if not isinstance(doc.get("completed_phases"), list):
        failures.append("completed_phases must be a list")
    return failures


def cmd_run_checkpoints(args: argparse.Namespace) -> int:
    doc = read_build_checkpoints(args.run_id)
    if doc is None:
        fail(f"No build checkpoints for run {args.run_id}")
        return 1
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    print(f"build checkpoints: {args.run_id}")
    for phase in doc["completed_phases"]:
        print(f"  ✓ {phase}")
    return 0
