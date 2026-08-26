from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._ancestry import impact_plan
from ._logging import fail, ok, warn
from ._operations import fenced_operation
from ._registry import _hash, _read_object, _root, change_artifact_status
from ._reports import _atomic_write_bytes

EVENT_SCHEMA = "https://ohbs-image.dev/rebuild-event/v1"
REBUILD_REQUEST_SCHEMA = "https://ohbs-image.dev/rebuild-request/v1"
_EVENT_TYPES = {"base_image.updated", "cve.detected"}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")


def validate_rebuild_event(event: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if event.get("schema") != EVENT_SCHEMA:
        failures.append("schema mismatch")
    if not _SAFE_ID.fullmatch(str(event.get("event_id") or "")):
        failures.append("event_id is missing or unsafe")
    if event.get("type") not in _EVENT_TYPES:
        failures.append("type must be base_image.updated or cve.detected")
    if not str(event.get("artifact_id") or ""):
        failures.append("artifact_id is required")
    try:
        datetime.fromisoformat(str(event.get("occurred_at") or "").replace("Z", "+00:00"))
    except ValueError:
        failures.append("occurred_at must be ISO-8601")
    if event.get("type") == "cve.detected" and not str(event.get("cve_id") or ""):
        failures.append("cve_id is required for cve.detected")
    return failures


def plan_rebuild_event(event: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    failures = validate_rebuild_event(event)
    if failures:
        raise ValueError("invalid rebuild event: " + "; ".join(failures))
    impact = impact_plan(str(event["artifact_id"]), root)
    reason = (f"{event['type']}:{event.get('cve_id') or event['event_id']}")
    plan: dict[str, Any] = {
        "schema": REBUILD_REQUEST_SCHEMA, "event_id": event["event_id"],
        "event_type": event["type"], "reason": reason, "impact": impact,
        "actions": [{"artifact_id": item["artifact_id"], "action": "quarantine_and_rebuild",
                     "current_status": item.get("status"), "depth": item["depth"]}
                    for item in impact["artifacts"] if item.get("status") == "active"],
        "mode": "dry-run",
    }
    plan["document_hash"] = _hash(plan)
    return plan


def process_rebuild_event(event: dict[str, Any], *, apply: bool = False,
                          actor: str = "event-controller",
                          root: Path | None = None) -> dict[str, Any]:
    plan = plan_rebuild_event(event, root)
    if not apply:
        return plan
    event_id = str(event["event_id"])
    with fenced_operation("rebuild-event", event_id, root=root) as claim:
        if claim["replay"]:
            result = claim.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"event {event_id} has no replayable result")
            return result
        results: list[dict[str, Any]] = []
        request_root = _root(root) / "rebuild_requests"
        for action in sorted(plan["actions"], key=lambda item: int(item["depth"]), reverse=True):
            artifact_id = str(action["artifact_id"])
            changed = change_artifact_status(
                artifact_id, "quarantined", actor=actor, reason=str(plan["reason"]),
                auto_rollback=True, root=root)
            request: dict[str, Any] = {
                "schema": REBUILD_REQUEST_SCHEMA, "request_id": f"{event_id}:{artifact_id}",
                "event_id": event_id, "artifact_id": artifact_id, "status": "queued",
                "reason": plan["reason"], "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "channel_rollbacks": changed["channel_rollbacks"],
            }
            request["document_hash"] = _hash(request)
            path = request_root / f"{_hash({'id': request['request_id']})}.json"
            _atomic_write_bytes(path, (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode())
            results.append(request)
        plan.update(mode="apply", results=results, queued=len(results),
                    processed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        plan["document_hash"] = _hash(plan)
        claim["result"] = plan
        return plan


def cmd_event_process(args: argparse.Namespace) -> int:
    try:
        event = _read_object(Path(args.event))
        if event is None:
            raise ValueError(f"invalid event document {args.event}")
        result = process_rebuild_event(event, apply=args.apply, actor=args.actor)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.apply:
        ok(f"Queued {result['queued']} rebuild request(s)")
    else:
        warn(f"Dry run: {len(result['actions'])} rebuild action(s); add --apply")
    return 0
