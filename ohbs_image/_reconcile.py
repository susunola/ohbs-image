from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._reports import _atomic_write_bytes
from ._run_events import append_run_event

RECONCILE_SCHEMA = "https://ohbs-image.dev/state-reconcile/v1"


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def plan_reconciliation(root: Path) -> dict[str, Any]:
    now = datetime.now(UTC)
    actions: list[dict[str, Any]] = []
    for path in sorted((root / "runs").glob("*.json")):
        doc = _read_object(path)
        if doc is None:
            actions.append({"action": "inspect_invalid_manifest", "run_id": path.stem,
                            "path": str(path), "safe_to_apply": False})
            continue
        run_id = str(doc.get("run_id") or path.stem)
        status = str(doc.get("status") or "")
        expiry = str(doc.get("lease_expires_at") or "")
        try:
            expired = bool(expiry) and datetime.fromisoformat(
                expiry.replace("Z", "+00:00")) <= now
        except ValueError:
            expired = False
        if status == "active" and expired:
            actions.append({"action": "expire_run", "run_id": run_id,
                            "path": str(path), "safe_to_apply": True,
                            "reason": "active lease expired"})
        resources = doc.get("resources")
        if status != "active" and isinstance(resources, list) and resources:
            actions.append({"action": "inspect_orphan_resources", "run_id": run_id,
                            "path": str(path), "safe_to_apply": False,
                            "resource_count": len(resources),
                            "reason": "terminal run still records ephemeral resources"})
    return {"schema": RECONCILE_SCHEMA, "path": str(root),
            "actions": actions, "count": len(actions),
            "safe_count": sum(bool(item["safe_to_apply"]) for item in actions)}


def apply_reconciliation(root: Path, plan: dict[str, Any]) -> int:
    applied = 0
    for action in plan["actions"]:
        if action["action"] != "expire_run" or not action["safe_to_apply"]:
            continue
        path = Path(str(action["path"]))
        doc = _read_object(path)
        if doc is None or doc.get("status") != "active":
            continue
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc.update({"status": "failed", "state": "FAILED", "phase": "lease-expired",
                    "updated_at": now, "lease_expires_at": now,
                    "next_action": f"ohbs-image run resume {action['run_id']}"})
        event = append_run_event(str(action["run_id"]), "FAILED", phase="lease-expired",
                                 reason="active lease expired", root=root)
        doc["event_sequence"] = event["sequence"]
        doc["event_hash"] = event["event_hash"]
        _atomic_write_bytes(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())
        applied += 1
    return applied


def cmd_state_reconcile(args: argparse.Namespace) -> int:
    root = _lineage_path().parent.expanduser().resolve()
    plan = plan_reconciliation(root)
    applied = apply_reconciliation(root, plan) if args.apply else 0
    plan["applied"] = applied
    if args.output == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    mode = "APPLY" if args.apply else "CHECK"
    print(f"state reconcile [{mode}]: {plan['count']} action(s), {applied} applied")
    for action in plan["actions"]:
        safety = "auto" if action["safe_to_apply"] else "manual"
        print(f"  {safety:6s} {action['action']:28s} {action['run_id']} — {action.get('reason', '')}")
    return 0
