from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._logging import ConfigError, fail, ok
from ._operations import fenced_operation, verify_fencing_token
from ._registry import _artifact_path, _hash, _read_object
from ._reports import _atomic_write_bytes, _state_lock

DISTRIBUTION_SCHEMA = "https://ohbs-image.dev/distribution-plan/v1"
EXECUTION_SCHEMA = "https://ohbs-image.dev/distribution-execution/v1"
SHARE_SCHEMA = "https://ohbs-image.dev/distribution-share/v1"
_REGION = re.compile(r"[a-z]{2,}-[a-z0-9-]{2,}")
CloudAPI = Callable[[str, str, str, str, dict[str, Any], str, str, str | None], dict[str, Any]]


def share_artifact(artifact_id: str, account_id: str, *, apply: bool = False,
                   root: Path | None = None, api: CloudAPI | None = None,
                   secret_id: str | None = None, secret_key: str | None = None,
                   token: str | None = None) -> dict[str, Any]:
    """Share an active image with another root account in its source region."""
    if not re.fullmatch(r"[0-9]{5,32}", account_id):
        raise ValueError("target account must be a Tencent Cloud root account ID")
    path, artifact = _load_artifact(artifact_id, root)
    result: dict[str, Any] = {"schema": SHARE_SCHEMA, "artifact_id": artifact_id,
                              "account_id": account_id,
                              "mode": "apply" if apply else "dry-run"}
    if not apply:
        return result
    if api is None:
        from ._tc_cloud import _tc3_api
        api = _tc3_api
    sid = secret_id or os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    skey = secret_key or os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    token = token or os.environ.get("TENCENTCLOUD_SECURITY_TOKEN")
    if not sid or not skey:
        raise ConfigError("source-account Tencent Cloud credentials are not set")
    response = api("cvm", "ModifyImageSharePermission", "2017-03-12",
        str(artifact.get("region") or ""), {"ImageId": artifact_id,
        "AccountIds": [account_id], "Permission": "SHARE"}, sid, skey, token)
    body = response.get("Response", {})
    if "Error" in body:
        raise ConfigError(f"ModifyImageSharePermission failed: {body['Error']}")
    lock = _state_lock(path)
    try:
        latest = _read_object(path)
        if latest is None or latest.get("document_hash") != _hash(latest):
            raise ValueError("artifact changed during image sharing")
        shares = dict(latest.get("shares") or {})
        shares[account_id] = {"status": "shared", "shared_at": datetime.now(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), "request_id": body.get("RequestId")}
        latest["shares"] = shares
        latest["document_hash"] = _hash(latest)
        _atomic_write_bytes(path, (json.dumps(latest, ensure_ascii=False, indent=2) + "\n").encode())
    finally:
        lock.rmdir()
    result["request_id"] = body.get("RequestId")
    return result


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
        elif isinstance(replica, dict) and replica.get("status") == "pending":
            action, reason = "skip", "copy-pending"
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
                   operation_id: str | None = None,
                   root: Path | None = None, _fencing_scope: str | None = None,
                   _fencing_token: int | None = None) -> dict[str, Any]:
    if operation_id is not None:
        scope = f"replica:{artifact_id}/{region}"
        with fenced_operation(scope, operation_id, root=root) as claim:
            if claim["replay"]:
                result = claim.get("result")
                if not isinstance(result, dict):
                    raise ValueError(f"operation {operation_id} has no replayable result")
                return result
            result = record_replica(
                artifact_id, region, replica_id, root=root, _fencing_scope=scope,
                _fencing_token=int(claim["token"]))
            claim["result"] = result
            return result
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
        if _fencing_scope is not None and _fencing_token is not None:
            verify_fencing_token(_fencing_scope, _fencing_token, root)
        artifact["replicas"] = replicas
        artifact["document_hash"] = _hash(artifact)
        _atomic_write_bytes(path,
                            (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode())
        return artifact
    finally:
        lock.rmdir()


def _set_replica(artifact_id: str, region: str, replica: dict[str, Any],
                 root: Path | None = None) -> dict[str, Any]:
    path, _artifact = _load_artifact(artifact_id, root)
    lock = _state_lock(path)
    try:
        artifact = _read_object(path)
        if artifact is None or artifact.get("document_hash") != _hash(artifact):
            raise ValueError(f"artifact {artifact_id} changed or failed integrity verification")
        replicas = artifact.get("replicas")
        replicas = dict(replicas) if isinstance(replicas, dict) else {}
        replicas[region] = replica
        artifact["replicas"] = replicas
        artifact["document_hash"] = _hash(artifact)
        _atomic_write_bytes(path, (json.dumps(artifact, ensure_ascii=False, indent=2) + "\n").encode())
        return artifact
    finally:
        lock.rmdir()


def execute_distribution(artifact_id: str, regions: list[str], *, apply: bool = False,
                         root: Path | None = None, api: CloudAPI | None = None,
                         secret_id: str | None = None,
                         secret_key: str | None = None,
                         token: str | None = None) -> dict[str, Any]:
    """Plan or start Tencent Cloud image synchronization.

    Cloud mutation is impossible unless ``apply`` is explicitly true. The API
    response is recorded as pending; readiness is only asserted by reconciliation.
    """
    plan = distribution_plan(artifact_id, regions, root)
    result: dict[str, Any] = {"schema": EXECUTION_SCHEMA, "mode": "apply" if apply else "dry-run",
                              "artifact_id": artifact_id, "plan": plan, "started": []}
    copies = [item for item in plan["actions"] if item["action"] == "copy"]
    if not apply or not copies:
        return result
    if api is None:
        from ._tc_cloud import _tc3_api
        api = _tc3_api
    sid = secret_id or os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    skey = secret_key or os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    token = token or os.environ.get("TENCENTCLOUD_SECURITY_TOKEN")
    if not sid or not skey:
        raise ConfigError("TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY not set")
    source_region = str(plan["source_region"])
    destinations = [str(item["region"]) for item in copies]
    response = api("cvm", "SyncImages", "2017-03-12", source_region,
                   {"ImageIds": [artifact_id], "DestinationRegions": destinations,
                    "ImageSetRequired": True}, sid, skey, token)
    body = response.get("Response", {})
    if "Error" in body:
        raise ConfigError(f"SyncImages failed: {body['Error']}")
    image_set = body.get("ImageSet") if isinstance(body.get("ImageSet"), list) else []
    ids = {str(item.get("Region")): str(item.get("ImageId")) for item in image_set
           if isinstance(item, dict) and item.get("Region") and item.get("ImageId")}
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for region in destinations:
        replica = {"replica_id": ids.get(region), "status": "pending", "initiated_at": now,
                   "request_id": body.get("RequestId"), "provider": "tencentcloud"}
        _set_replica(artifact_id, region, replica, root)
        result["started"].append({"region": region, **replica})
    return result


def reconcile_distribution(artifact_id: str, *, root: Path | None = None,
                           timeout_minutes: int = 60, api: CloudAPI | None = None,
                           secret_id: str | None = None,
                           secret_key: str | None = None,
                           token: str | None = None,
                           now: datetime | None = None) -> dict[str, Any]:
    if timeout_minutes < 1:
        raise ValueError("timeout_minutes must be at least 1")
    _path, artifact = _load_artifact(artifact_id, root)
    raw_replicas = artifact.get("replicas")
    replicas: dict[str, Any] = raw_replicas if isinstance(raw_replicas, dict) else {}
    pending = [(str(region), replica) for region, replica in replicas.items()
               if isinstance(replica, dict) and replica.get("status") == "pending"]
    result: dict[str, Any] = {"artifact_id": artifact_id, "checked": 0,
                              "ready": 0, "pending": 0, "failed": 0, "replicas": []}
    if not pending:
        return result
    if api is None:
        from ._tc_cloud import _tc3_api
        api = _tc3_api
    sid = secret_id or os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    skey = secret_key or os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    token = token or os.environ.get("TENCENTCLOUD_SECURITY_TOKEN")
    if not sid or not skey:
        raise ConfigError("TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY not set")
    current_time = now or datetime.now(UTC)
    for region, replica in pending:
        replica_id = str(replica.get("replica_id") or "")
        state = "pending"
        if replica_id:
            response = api("cvm", "DescribeImages", "2017-03-12", region,
                           {"ImageIds": [replica_id]}, sid, skey, token)
            body = response.get("Response", {})
            if "Error" in body:
                raise ConfigError(f"DescribeImages failed in {region}: {body['Error']}")
            images = body.get("ImageSet") if isinstance(body.get("ImageSet"), list) else []
            if images and str(images[0].get("ImageState")) == "NORMAL":
                state = "ready"
        try:
            initiated = datetime.fromisoformat(str(replica.get("initiated_at", "")).replace("Z", "+00:00"))
        except ValueError:
            initiated = current_time
        if state == "pending" and current_time - initiated >= timedelta(minutes=timeout_minutes):
            state = "failed"
        updated = dict(replica, status=state,
                       reconciled_at=current_time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if state == "failed":
            updated["failure_category"] = "replica-timeout"
        _set_replica(artifact_id, region, updated, root)
        result["checked"] += 1
        result[state] += 1
        result["replicas"].append({"region": region, **updated})
    return result


def cmd_distribution_plan(args: argparse.Namespace) -> int:
    try:
        plan = distribution_plan(args.artifact_id, args.region)
    except (ConfigError, OSError, ValueError) as exc:
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
        record_replica(args.artifact_id, args.region, args.replica_id,
                       operation_id=args.operation_id)
    except (ConfigError, OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    ok(f"Recorded {args.artifact_id} replica {args.replica_id} in {args.region}")
    return 0


def cmd_distribution_execute(args: argparse.Namespace) -> int:
    try:
        result = execute_distribution(args.artifact_id, args.region, apply=args.apply)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"distribution {result['mode']}: {len(result['started'])} copy request(s) started")
    return 0


def cmd_distribution_reconcile(args: argparse.Namespace) -> int:
    try:
        result = reconcile_distribution(args.artifact_id, timeout_minutes=args.timeout_minutes)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"checked {result['checked']}: ready={result['ready']} "
              f"pending={result['pending']} failed={result['failed']}")
    return 1 if result["failed"] else 0
