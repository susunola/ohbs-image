from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import fail, ok
from ._registry import _artifact_path, _hash, _read_object
from ._reports import _atomic_write_bytes, _state_lock

DISTRIBUTION_SCHEMA = "https://ohbs-image.dev/distribution-plan/v1"
_REGION = re.compile(r"[a-z]{2,}-[a-z0-9-]{2,}")


def _load_artifact(artifact_id: str, root: Path | None = None) -> tuple[Path, dict[str, Any]]:
    path = _artifact_path(artifact_id, root)
    doc = _read_object(path)
    if doc is None or doc.get("document_hash") != _hash(doc):
        raise ValueError(f"artifact {artifact_id} not found or failed integrity verification")
    if doc.get("status") != "active":
        raise ValueError(f"artifact {artifact_id} is not active")
    return path, doc


def distribution_plan(artifact_id: str, regions: list[str],
                      root: Path | None = None) -> dict[str, Any]:
    _path, artifact = _load_artifact(artifact_id, root)
    replicas = artifact.get("replicas")
    replicas = replicas if isinstance(replicas, dict) else {}
    source_region = str(artifact.get("region") or "")
    actions: list[dict[str, Any]] = []
    for region in dict.fromkeys(regions):
        if not _REGION.fullmatch(region):
            raise ValueError(f"invalid region {region!r}")
        replica = replicas.get(region)
        if region == source_region:
            action, reason = "skip", "source-region"
        elif isinstance(replica, dict) and replica.get("status") == "ready":
            action, reason = "skip", "cache-hit"
        else:
            action, reason = "copy", "replica-missing"
        cache_key = hashlib.sha256(
            f"{artifact.get('bucket')}:{artifact.get('version')}:"
            f"{artifact_id}:{region}".encode()).hexdigest()
        actions.append({"region": region, "action": action, "reason": reason,
                        "cache_key": cache_key,
                        "replica_id": replica.get("replica_id")
                        if isinstance(replica, dict) else None})
    plan: dict[str, Any] = {
        "schema": DISTRIBUTION_SCHEMA, "artifact_id": artifact_id,
        "source_region": source_region,
        "copy_count": sum(item["action"] == "copy" for item in actions),
        "cache_hits": sum(item["reason"] == "cache-hit" for item in actions),
        "actions": actions,
    }
    plan["document_hash"] = _hash(plan)
    return plan


def record_replica(artifact_id: str, region: str, replica_id: str, *,
                   root: Path | None = None) -> dict[str, Any]:
    if not _REGION.fullmatch(region):
        raise ValueError(f"invalid region {region!r}")
    if not replica_id.strip():
        raise ValueError("replica_id is required")
    path, _artifact = _load_artifact(artifact_id, root)
    lock = _state_lock(path)
    try:
        artifact = _read_object(path)
        if artifact is None or artifact.get("document_hash") != _hash(artifact):
            raise ValueError(f"artifact {artifact_id} changed or failed integrity verification")
        replicas = artifact.get("replicas")
        replicas = dict(replicas) if isinstance(replicas, dict) else {}
        current = replicas.get(region)
        if isinstance(current, dict) and current.get("status") == "ready":
            if current.get("replica_id") != replica_id:
                raise ValueError(f"region {region} already has replica {current.get('replica_id')}")
            return artifact
        replicas[region] = {
            "replica_id": replica_id, "status": "ready",
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        artifact["replicas"] = replicas
        artifact["document_hash"] = _hash(artifact)
        _atomic_write_bytes(path,
                            (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode())
        return artifact
    finally:
        lock.rmdir()


def cmd_distribution_plan(args: argparse.Namespace) -> int:
    try:
        plan = distribution_plan(args.artifact_id, args.region)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        for item in plan["actions"]:
            print(f"{item['action']:4s} {item['region']}: {item['reason']}")
    return 0


def cmd_distribution_record(args: argparse.Namespace) -> int:
    try:
        record_replica(args.artifact_id, args.region, args.replica_id)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    ok(f"Recorded {args.artifact_id} replica {args.replica_id} in {args.region}")
    return 0
