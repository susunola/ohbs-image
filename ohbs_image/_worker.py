from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail, info, ok, warn
from ._registry import _hash, _read_object
from ._reports import _atomic_write_bytes, _state_lock
from ._state_db import StateDatabase

WORKER_RESULT_SCHEMA = "https://ohbs-image.dev/rebuild-worker-result/v1"
_TERMINAL = {"succeeded", "dead_letter"}
_REQUIRED_STAGES = ("build", "policy", "distribute", "promote")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_request(path: Path, request: dict[str, Any]) -> None:
    request["document_hash"] = _hash(request)
    _atomic_write_bytes(
        path, (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode())


def _valid_result(result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if result.get("schema") != WORKER_RESULT_SCHEMA:
        failures.append("worker result schema mismatch")
    stages = result.get("stages")
    if not isinstance(stages, dict):
        return [*failures, "worker result stages are required"]
    for stage in _REQUIRED_STAGES:
        item = stages.get(stage)
        if not isinstance(item, dict) or item.get("status") != "succeeded":
            failures.append(f"stage {stage} did not succeed")
    if not str(result.get("artifact_id") or ""):
        failures.append("worker result artifact_id is required")
    return failures


def _eligible(request: dict[str, Any], now: datetime) -> bool:
    status = request.get("status")
    if status == "queued":
        return True
    if status == "retry_wait":
        try:
            retry_at = datetime.fromisoformat(
                str(request.get("next_attempt_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return True
        return retry_at <= now
    if status == "running":
        try:
            lease = datetime.fromisoformat(
                str(request.get("lease_expires_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return True
        return lease <= now
    return False


def claim_request(queue: Path, worker_id: str, *, lease_seconds: int = 900,
                  now: datetime | None = None) -> tuple[Path, dict[str, Any]] | None:
    current = now or _now()
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(queue / ".worker-claim")
    try:
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(queue.glob("*.json")):
            request = _read_object(path)
            if request is not None and _eligible(request, current):
                candidates.append((path, request))
        if not candidates:
            return None
        path, request = candidates[0]
        attempt = int(request.get("attempt", 0)) + 1
        history = request.get("worker_history")
        events = list(history) if isinstance(history, list) else []
        events.append({"status": "running", "at": _stamp(current),
                       "worker_id": worker_id, "attempt": attempt})
        request.update(status="running", attempt=attempt, worker_id=worker_id,
                       claimed_at=_stamp(current),
                       lease_expires_at=_stamp(current + timedelta(seconds=lease_seconds)),
                       worker_history=events)
        _write_request(path, request)
        return path, request
    finally:
        lock.rmdir()


def finish_request(path: Path, request: dict[str, Any], *,
                   result: dict[str, Any] | None = None,
                   error: str = "", max_attempts: int = 3,
                   retry_delay_seconds: int = 60,
                   now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    lock = _state_lock(path)
    try:
        latest = _read_object(path)
        if latest is None or latest.get("worker_id") != request.get("worker_id"):
            raise ValueError("rebuild request lease ownership changed")
        history = latest.get("worker_history")
        events = list(history) if isinstance(history, list) else []
        if result is not None:
            failures = _valid_result(result)
            if failures:
                error = "; ".join(failures)
            else:
                latest.update(status="succeeded", result=result,
                              completed_at=_stamp(current), error="")
                events.append({"status": "succeeded", "at": _stamp(current),
                               "worker_id": latest.get("worker_id"),
                               "attempt": latest.get("attempt")})
        if latest.get("status") != "succeeded":
            attempt = int(latest.get("attempt", 1))
            terminal = attempt >= max_attempts
            status = "dead_letter" if terminal else "retry_wait"
            latest.update(status=status, error=error or "worker handler failed",
                          failed_at=_stamp(current))
            if not terminal:
                latest["next_attempt_at"] = _stamp(
                    current + timedelta(seconds=retry_delay_seconds * (2 ** (attempt - 1))))
            events.append({"status": status, "at": _stamp(current),
                           "worker_id": latest.get("worker_id"), "attempt": attempt,
                           "error": latest["error"]})
        latest["worker_history"] = events
        latest.pop("lease_expires_at", None)
        _write_request(path, latest)
        return latest
    finally:
        lock.rmdir()


def process_one(queue: Path, handler: Callable[[dict[str, Any]], dict[str, Any]], *,
                worker_id: str, max_attempts: int = 3,
                lease_seconds: int = 900, retry_delay_seconds: int = 60
                ) -> dict[str, Any] | None:
    claimed = claim_request(queue, worker_id, lease_seconds=lease_seconds)
    if claimed is None:
        return None
    path, request = claimed
    try:
        result = handler(request)
        return finish_request(path, request, result=result, max_attempts=max_attempts,
                              retry_delay_seconds=retry_delay_seconds)
    except Exception as exc:  # handler boundary must persist failure before returning
        return finish_request(path, request, error=str(exc), max_attempts=max_attempts,
                              retry_delay_seconds=retry_delay_seconds)


def process_one_db(database: StateDatabase,
                   handler: Callable[[dict[str, Any]], dict[str, Any]], *,
                   worker_id: str, max_attempts: int = 3, lease_seconds: int = 900,
                   retry_delay_seconds: int = 60) -> dict[str, Any] | None:
    request = database.claim(worker_id, lease_seconds=lease_seconds)
    if request is None:
        return None
    current = _now()
    document = dict(request)
    events = list(document.get("worker_history") or [])
    try:
        result = handler(request)
        failures = _valid_result(result)
        if failures:
            raise ValueError("; ".join(failures))
        document.update(status="succeeded", result=result, completed_at=_stamp(current), error="")
        events.append({"status": "succeeded", "at": _stamp(current),
                       "worker_id": worker_id, "attempt": document["attempt"]})
    except Exception as exc:
        attempt = int(document.get("attempt", 1))
        status = "dead_letter" if attempt >= max_attempts else "retry_wait"
        document.update(status=status, error=str(exc) or "worker handler failed",
                        failed_at=_stamp(current))
        if status == "retry_wait":
            document["next_attempt_at"] = _stamp(
                current + timedelta(seconds=retry_delay_seconds * (2 ** (attempt - 1))))
        events.append({"status": status, "at": _stamp(current), "worker_id": worker_id,
                       "attempt": attempt, "error": document["error"]})
    document["worker_history"] = events
    document.pop("lease_expires_at", None)
    database.finish(request, document)
    return document


def _command_handler(command: str, timeout: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("worker handler command is empty")

    def run(request: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            argv, input=json.dumps(request), text=True, capture_output=True,
            timeout=timeout, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"handler exited {completed.returncode}: {completed.stderr[-2000:].strip()}")
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("worker handler must return one JSON object")
        return result

    return run


def cmd_worker_run(args: argparse.Namespace) -> int:
    queue = _lineage_path().parent / "registry" / "rebuild_requests"
    database = StateDatabase(Path(args.state_db)) if getattr(args, "state_db", "") else None
    if not args.apply:
        if database is not None:
            database.initialize()
            info(f"Dry run: transactional queue ready at {database.path}; add --apply")
            return 0
        eligible = sum(1 for path in queue.glob("*.json")
                       if (request := _read_object(path)) is not None
                       and _eligible(request, _now()))
        warn(f"Dry run: {eligible} rebuild request(s) eligible; add --apply")
        return 0
    worker_id = args.worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    try:
        handler = _command_handler(args.handler, args.timeout)
        while True:
            if database is None:
                result = process_one(queue, handler, worker_id=worker_id,
                                     max_attempts=args.max_attempts,
                                     lease_seconds=args.lease_seconds,
                                     retry_delay_seconds=args.retry_delay_seconds)
            else:
                result = process_one_db(database, handler, worker_id=worker_id,
                                        max_attempts=args.max_attempts,
                                        lease_seconds=args.lease_seconds,
                                        retry_delay_seconds=args.retry_delay_seconds)
            if result is None:
                if args.once:
                    info("No eligible rebuild requests")
                    return 0
                time.sleep(args.poll_seconds)
                continue
            status = str(result.get("status"))
            if status == "succeeded":
                ok(f"Rebuild request {result.get('request_id')} succeeded")
            else:
                fail(f"Rebuild request {result.get('request_id')} is {status}")
            if args.once:
                return 0 if status in _TERMINAL or status == "retry_wait" else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        fail(str(exc))
        return 2
