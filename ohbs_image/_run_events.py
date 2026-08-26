from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail

RUN_EVENT_SCHEMA = "https://ohbs-image.dev/run-event/v1"
RUN_EVENTS_SCHEMA = "https://ohbs-image.dev/run-events/v1"

RUN_STATES = {
    "CREATED", "DIAGNOSING", "PLANNED", "PREFLIGHT_PASSED", "READY",
    "BUILDING", "VERIFYING", "EVIDENCE_READY", "APPROVED", "DISTRIBUTED",
    "WAITING_APPROVAL", "RETRYING", "FAILED", "CANCELLED", "TIMED_OUT", "REVOKED",
}
TERMINAL_STATES = {"FAILED", "CANCELLED", "TIMED_OUT", "REVOKED", "DISTRIBUTED"}
_TRANSITIONS = {
    "CREATED": {"DIAGNOSING", "PLANNED", "PREFLIGHT_PASSED", "READY", "BUILDING", "VERIFYING", "FAILED", "CANCELLED"},
    "DIAGNOSING": {"PLANNED", "FAILED", "CANCELLED"},
    "PLANNED": {"PREFLIGHT_PASSED", "READY", "FAILED", "CANCELLED"},
    "PREFLIGHT_PASSED": {"READY", "BUILDING", "FAILED", "CANCELLED"},
    "READY": {"BUILDING", "WAITING_APPROVAL", "FAILED", "CANCELLED"},
    "WAITING_APPROVAL": {"READY", "BUILDING", "CANCELLED"},
    "RETRYING": {"DIAGNOSING", "PLANNED", "PREFLIGHT_PASSED", "BUILDING", "VERIFYING", "FAILED"},
    "BUILDING": {"VERIFYING", "EVIDENCE_READY", "APPROVED", "FAILED", "TIMED_OUT", "CANCELLED"},
    "VERIFYING": {"EVIDENCE_READY", "APPROVED", "FAILED", "TIMED_OUT", "CANCELLED"},
    "EVIDENCE_READY": {"APPROVED", "DISTRIBUTED", "FAILED", "CANCELLED"},
    "APPROVED": {"DISTRIBUTED", "REVOKED"},
    "FAILED": {"RETRYING"},
    "TIMED_OUT": {"RETRYING"},
    "CANCELLED": {"RETRYING"},
    "DISTRIBUTED": {"REVOKED"},
    "REVOKED": set(),
}


def state_for_manifest(status: str, phase: str) -> str:
    if status == "failed":
        return "FAILED"
    if status == "ready":
        return "READY"
    if status == "completed":
        return "APPROVED" if phase == "release-complete" else "EVIDENCE_READY"
    if phase == "launch-doctor":
        return "DIAGNOSING"
    if phase == "launch-plan":
        return "PLANNED"
    if phase == "launch-preflight":
        return "PREFLIGHT_PASSED"
    if phase.startswith("probe-"):
        return "VERIFYING"
    if phase in {"launch-build", "packer-build"}:
        return "BUILDING"
    return "CREATED"


def _event_path(run_id: str, root: Path | None = None) -> Path:
    return (root or _lineage_path().parent) / "events" / f"{run_id}.jsonl"


def read_run_events(run_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    path = _event_path(run_id, root)
    events: list[dict[str, Any]] = []
    with suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _canonical_hash(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock(path: Path) -> Path:
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + 10
    while True:
        try:
            lock.mkdir(mode=0o700)
            return lock
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(f"timed out waiting for event lock {lock}") from None
            time.sleep(0.05)


def _write_event(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_run_event(run_id: str, state: str, *, phase: str = "", reason: str = "",
                     metadata: dict[str, Any] | None = None, actor: str = "",
                     root: Path | None = None) -> dict[str, Any]:
    if state not in RUN_STATES:
        raise ValueError(f"unknown run state {state}")
    path = _event_path(run_id, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _lock(path)
    try:
        events = read_run_events(run_id, root)
        if not events and state != "CREATED":
            created: dict[str, Any] = {
                "schema": RUN_EVENT_SCHEMA, "event_id": str(uuid.uuid4()),
                "run_id": run_id, "sequence": 1,
                "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "from": None, "to": "CREATED", "phase": "run-created",
                "actor": actor or os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown",
                "reason": "", "metadata": {}, "previous_hash": "",
            }
            created["event_hash"] = _canonical_hash(created)
            _write_event(path, created)
            events = [created]
        previous = str(events[-1].get("to") or "") if events else ""
        if previous and state != previous and state not in _TRANSITIONS.get(previous, set()):
            raise ValueError(f"illegal run transition {previous} -> {state}")
        event: dict[str, Any] = {
            "schema": RUN_EVENT_SCHEMA,
            "event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "sequence": len(events) + 1,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from": previous or None,
            "to": state,
            "phase": phase,
            "actor": actor or os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown",
            "reason": reason,
            "metadata": metadata or {},
            "previous_hash": str(events[-1].get("event_hash") or "") if events else "",
        }
        event["event_hash"] = _canonical_hash(event)
        _write_event(path, event)
        return event
    finally:
        with suppress(OSError):
            lock.rmdir()


def verify_event_chain(run_id: str, root: Path | None = None) -> list[str]:
    events = read_run_events(run_id, root)
    failures: list[str] = []
    previous_hash = ""
    previous_state = ""
    for expected_sequence, event in enumerate(events, 1):
        if event.get("sequence") != expected_sequence:
            failures.append(f"sequence {expected_sequence} is missing or reordered")
        if event.get("previous_hash") != previous_hash:
            failures.append(f"event {expected_sequence} previous_hash mismatch")
        if event.get("event_hash") != _canonical_hash(event):
            failures.append(f"event {expected_sequence} hash mismatch")
        state = str(event.get("to") or "")
        if state not in RUN_STATES:
            failures.append(f"event {expected_sequence} has unknown state {state}")
        if event.get("from") != (previous_state or None):
            failures.append(f"event {expected_sequence} from-state mismatch")
        if previous_state and state != previous_state and state not in _TRANSITIONS.get(previous_state, set()):
            failures.append(f"event {expected_sequence} illegal transition {previous_state} -> {state}")
        previous_hash = str(event.get("event_hash") or "")
        previous_state = state
    return failures


def cmd_run_events(args: argparse.Namespace) -> int:
    events = read_run_events(args.run_id)
    if not events:
        fail(f"No event log for run {args.run_id}")
        return 1
    if args.output == "json":
        print(json.dumps({"schema": RUN_EVENTS_SCHEMA, "run_id": args.run_id,
                          "count": len(events), "events": events}, ensure_ascii=False, indent=2))
        return 0
    for event in events:
        print(f"{event['sequence']:3d}  {event['timestamp']}  "
              f"{str(event.get('from') or '-'):17s} -> {event['to']:17s}  {event['phase']}")
    return 0
