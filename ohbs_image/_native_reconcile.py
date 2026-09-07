"""Safe cleanup-only reconciliation for retained or orphaned native resources."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from ._config import load_config, load_config_layered, resolve
from ._logging import ConfigError, fail
from ._reports import _atomic_write_bytes
from ._tc_cloud import _creds, _tc3_api
from .native.journal import read_journal
from .native.providers.tencentcloud import teardown_keypair, terminate

RECONCILE_SCHEMA = "https://ohbs-image.dev/native-resource-reconcile/v1"


def _body(response: dict[str, Any], action: str) -> dict[str, Any]:
    body = response.get("Response")
    if not isinstance(body, dict):
        raise ConfigError(f"{action} returned a malformed response")
    if body.get("Error"):
        raise ConfigError(f"{action} failed: {body['Error']}")
    return body


def _tags(instance: dict[str, Any]) -> dict[str, str]:
    return {str(item.get("Key") or ""): str(item.get("Value") or "")
            for item in instance.get("Tags") or [] if isinstance(item, dict)}


def _discover_instances(runtime: Any, run_id: str) -> list[dict[str, Any]]:
    sid, skey, token = _creds(
        runtime.secret_id_env, runtime.secret_key_env, runtime.security_token_env)
    params: dict[str, Any] = {"Filters": [
        {"Name": "tag:managed_by", "Values": ["ohbs-image"]},
        {"Name": "tag:ephemeral", "Values": ["true"]},
        {"Name": "tag:run_id", "Values": [run_id]},
    ], "Limit": 100, "Offset": 0}
    found: list[dict[str, Any]] = []
    while True:
        response = _tc3_api("cvm", "DescribeInstances", "2017-03-12", runtime.region,
                            params, sid, skey, token or None)
        batch = _body(response, "DescribeInstances").get("InstanceSet") or []
        for item in batch:
            if not isinstance(item, dict):
                continue
            tags = _tags(item)
            if (tags.get("managed_by") == "ohbs-image" and
                    tags.get("ephemeral") == "true" and
                    tags.get("run_id") == run_id):
                found.append(item)
        if len(batch) < params["Limit"]:
            break
        params["Offset"] += params["Limit"]
    return found


def _discover_expired_instances(runtime: Any, now_epoch: int) -> list[dict[str, Any]]:
    """Discover only explicitly OHBS-owned ephemeral CVMs with expired leases."""
    sid, skey, token = _creds(
        runtime.secret_id_env, runtime.secret_key_env, runtime.security_token_env)
    params: dict[str, Any] = {"Filters": [
        {"Name": "tag:managed_by", "Values": ["ohbs-image"]},
        {"Name": "tag:ephemeral", "Values": ["true"]},
    ], "Limit": 100, "Offset": 0}
    expired: list[dict[str, Any]] = []
    while True:
        response = _tc3_api("cvm", "DescribeInstances", "2017-03-12", runtime.region,
                            params, sid, skey, token or None)
        batch = _body(response, "DescribeInstances").get("InstanceSet") or []
        for item in batch:
            if not isinstance(item, dict):
                continue
            tags = _tags(item)
            try:
                expiry = int(tags.get("lease_expires_epoch") or 0)
            except ValueError:
                expiry = 0
            if (tags.get("managed_by") == "ohbs-image" and
                    tags.get("ephemeral") == "true" and tags.get("run_id") and
                    expiry > 0 and expiry <= now_epoch):
                expired.append(item)
        if len(batch) < params["Limit"]:
            break
        params["Offset"] += params["Limit"]
    return expired


def sweep_expired_resources(runtime: Any, workdir: Path, *, apply: bool = False,
                            now_epoch: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    instances = _discover_expired_instances(runtime, now)
    actions = []
    for instance in instances:
        resource: dict[str, Any] = {"type": "instance", "id": str(instance.get("InstanceId") or ""),
                    "state": str(instance.get("InstanceState") or ""),
                    "run_id": _tags(instance).get("run_id", ""),
                    "lease_expires_epoch": int(_tags(instance)["lease_expires_epoch"])}
        if not apply:
            actions.append({"action": "terminate", "resource": resource, "status": "planned"})
            continue
        try:
            terminate(runtime, resource["id"])
            actions.append({"action": "terminate", "resource": resource, "status": "completed"})
        except Exception as exc:
            actions.append({"action": "terminate", "resource": resource, "status": "failed",
                            "error_type": type(exc).__name__})
    failures = sum(item["status"] == "failed" for item in actions)
    document = {"schema": RECONCILE_SCHEMA, "operation": "expired-lease-sweep",
                "region": runtime.region, "mode": "apply" if apply else "check",
                "observed_at_epoch": now, "resources": len(instances),
                "actions": actions, "failed": failures,
                "status": "failed" if failures else "completed"}
    target = workdir / "native" / f"expired-lease-sweep-{now}.json"
    _atomic_write_bytes(target, (json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    document["evidence_path"] = str(target)
    return document


def _local_key_candidate(workdir: Path, run_id: str) -> tuple[str, str]:
    journal = read_journal(workdir)
    if journal and journal.get("run_id") == run_id:
        return str(journal.get("key_id") or ""), str(journal.get("key_path") or "")
    try:
        record = json.loads((workdir / "native" / "build-record.json").read_text())
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(record, dict) or record.get("run_id") != run_id:
        return "", ""
    for item in (record.get("cleanup") or {}).get("remaining_resources") or []:
        if isinstance(item, dict) and item.get("type") == "keypair":
            return str(item.get("id") or ""), ""
    return "", ""


def _validate_key(runtime: Any, key_id: str) -> dict[str, Any] | None:
    if not key_id:
        return None
    sid, skey, token = _creds(
        runtime.secret_id_env, runtime.secret_key_env, runtime.security_token_env)
    response = _tc3_api("cvm", "DescribeKeyPairs", "2017-03-12", runtime.region,
                        {"KeyIds": [key_id]}, sid, skey, token or None)
    values = _body(response, "DescribeKeyPairs").get("KeyPairSet") or []
    exact = [item for item in values if isinstance(item, dict)
             and str(item.get("KeyId") or "") == key_id]
    if not exact:
        return None
    if len(exact) != 1 or not str(exact[0].get("KeyName") or "").startswith("ohbs_"):
        raise ConfigError(f"key pair {key_id} does not have an OHBS-owned identity")
    return exact[0]


def reconcile_native_resources(runtime: Any, workdir: Path, run_id: str,
                               *, apply: bool = False) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id) is None:
        raise ConfigError("native reconcile requires a safe bounded run ID")
    started = time.time()
    instances = _discover_instances(runtime, run_id)
    key_id, key_path = _local_key_candidate(workdir, run_id)
    key = _validate_key(runtime, key_id)
    resources = [
        {"type": "instance", "id": str(item.get("InstanceId") or ""),
         "state": str(item.get("InstanceState") or ""), "ownership": "cloud-tags"}
        for item in instances if str(item.get("InstanceId") or "")
    ]
    if key:
        resources.append({"type": "keypair", "id": key_id,
                          "state": "present", "ownership": "local-record+cloud-id"})
    actions: list[dict[str, Any]] = []
    instance_failed = False
    if apply:
        for item in [value for value in resources if value["type"] == "instance"]:
            try:
                terminate(runtime, item["id"])
                actions.append({"action": "terminate", "resource": item, "status": "completed"})
            except Exception as exc:
                instance_failed = True
                actions.append({"action": "terminate", "resource": item, "status": "failed",
                                "error_type": type(exc).__name__})
        if key and not instance_failed:
            try:
                teardown_keypair(runtime, key_id, key_path)
                actions.append({"action": "delete-keypair", "resource": resources[-1],
                                "status": "completed"})
            except Exception as exc:
                actions.append({"action": "delete-keypair", "resource": resources[-1],
                                "status": "failed", "error_type": type(exc).__name__})
    else:
        actions = [{"action": "terminate" if item["type"] == "instance" else "delete-keypair",
                    "resource": item, "status": "planned"} for item in resources]
    failed = sum(item["status"] == "failed" for item in actions)
    document = {
        "schema": RECONCILE_SCHEMA, "run_id": run_id, "region": runtime.region,
        "mode": "apply" if apply else "check", "resources": resources,
        "actions": actions, "resource_count": len(resources), "failed": failed,
        "status": "failed" if failed else "completed",
        "started_at_epoch": int(started), "finished_at_epoch": int(time.time()),
    }
    target = workdir / "native" / f"reconcile-{run_id}.json"
    _atomic_write_bytes(target, (json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
    document["evidence_path"] = str(target)
    return document


def cmd_native_reconcile(args: argparse.Namespace) -> int:
    try:
        paths = [Path(args.config), *[Path(value) for value in (args.overlay or [])]]
        runtime = resolve(load_config_layered(paths) if len(paths) > 1
                          else load_config(paths[0]))
        runtime.run_id = args.run_id
        result = reconcile_native_resources(
            runtime, Path(args.workdir).resolve(), args.run_id,
            apply=bool(args.apply and not args.dry_run))
    except ConfigError as exc:
        fail(str(exc))
        return 1
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"native reconcile [{result['mode'].upper()}]: "
              f"{result['resource_count']} resource(s), {result['failed']} failed")
        for action in result["actions"]:
            resource = action["resource"]
            print(f"  {action['status']:9s} {action['action']:14s} "
                  f"{resource['type']} {resource['id']}")
        print(f"  evidence: {result['evidence_path']}")
    return 1 if result["failed"] else 0


def cmd_native_sweep(args: argparse.Namespace) -> int:
    try:
        paths = [Path(args.config), *[Path(value) for value in (args.overlay or [])]]
        runtime = resolve(load_config_layered(paths) if len(paths) > 1
                          else load_config(paths[0]))
        result = sweep_expired_resources(
            runtime, Path(args.workdir).resolve(), apply=bool(args.apply and not args.dry_run))
    except ConfigError as exc:
        fail(str(exc))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["failed"] else 0
