from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._config import _state_dir
from ._distribution import execute_distribution, reconcile_distribution, share_artifact
from ._logging import ConfigError, fail, info, warn
from ._registry import collect_artifacts

QUEUE_SCHEMA = "https://ohbs-image.dev/distribution-job/v1"
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


class DistributionQueue:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS distribution_jobs (
                    job_id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL,
                    document TEXT NOT NULL, status TEXT NOT NULL,
                    target_account TEXT NOT NULL, target_region TEXT NOT NULL,
                    worker_id TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_distribution_claim
                    ON distribution_jobs(status,target_account,target_region,created_at);
            """)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def enqueue(self, artifact_id: str, region: str, *, account: str = "self",
                mode: str = "sync") -> dict[str, Any]:
        if mode not in {"sync", "share"} or not _SAFE.fullmatch(account):
            raise ValueError("distribution mode or target account is invalid")
        dedupe = f"{mode}:{artifact_id}:{account}:{region}"
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT document FROM distribution_jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
            if existing is not None:
                return dict(json.loads(existing["document"]))
            now = _stamp()
            document = {"schema": QUEUE_SCHEMA, "job_id": f"dist-{uuid.uuid4().hex[:16]}",
                        "artifact_id": artifact_id, "target_region": region,
                        "target_account": account, "mode": mode, "status": "queued",
                        "attempt": 0, "created_at": now}
            connection.execute("INSERT INTO distribution_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (document["job_id"], dedupe, json.dumps(document), "queued", account, region,
                 None, None, now, now))
            return document

    def claim(self, worker_id: str, *, global_limit: int, account_limit: int,
              region_limit: int, lease_seconds: int = 900) -> dict[str, Any] | None:
        if min(global_limit, account_limit, region_limit, lease_seconds) < 1:
            raise ValueError("distribution quotas and lease must be positive")
        now = datetime.now(UTC)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE distribution_jobs SET status='queued',worker_id=NULL "
                    "WHERE status='running' AND lease_expires_at<=?", (_stamp(now),))
                running = int(connection.execute(
                    "SELECT count(*) FROM distribution_jobs WHERE status='running'").fetchone()[0])
                if running >= global_limit:
                    connection.commit()
                    return None
                candidates = connection.execute(
                    "SELECT * FROM distribution_jobs WHERE status='queued' ORDER BY created_at"
                ).fetchall()
                row = None
                for candidate in candidates:
                    account_running = int(connection.execute(
                        "SELECT count(*) FROM distribution_jobs WHERE status='running' AND target_account=?",
                        (candidate["target_account"],)).fetchone()[0])
                    region_running = int(connection.execute(
                        "SELECT count(*) FROM distribution_jobs WHERE status='running' AND target_region=?",
                        (candidate["target_region"],)).fetchone()[0])
                    if account_running < account_limit and region_running < region_limit:
                        row = candidate
                        break
                if row is None:
                    connection.commit()
                    return None
                document = json.loads(row["document"])
                document.update(status="running", worker_id=worker_id,
                                attempt=int(document.get("attempt", 0)) + 1,
                                claimed_at=_stamp(now))
                lease = _stamp(now + timedelta(seconds=lease_seconds))
                connection.execute("UPDATE distribution_jobs SET document=?,status='running',"
                    "worker_id=?,lease_expires_at=?,updated_at=? WHERE job_id=?",
                    (json.dumps(document), worker_id, lease, _stamp(now), row["job_id"]))
                connection.commit()
                return dict(document)
            except Exception:
                connection.rollback()
                raise

    def finish(self, job: dict[str, Any], *, result: dict[str, Any] | None = None,
               error: str = "", max_attempts: int = 3) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM distribution_jobs WHERE job_id=?",
                                     (job["job_id"],)).fetchone()
            if row is None or row["worker_id"] != job.get("worker_id"):
                connection.rollback()
                raise ValueError("distribution lease ownership changed")
            document = dict(job)
            if result is not None:
                document.update(status="succeeded", result=result, completed_at=_stamp(), error="")
            else:
                terminal = int(document.get("attempt", 1)) >= max_attempts
                document.update(status="dead_letter" if terminal else "queued", error=error,
                                failed_at=_stamp())
            connection.execute("UPDATE distribution_jobs SET document=?,status=?,worker_id=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE job_id=?",
                (json.dumps(document), document["status"], _stamp(), document["job_id"]))
            connection.commit()
            return document

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return {str(row["status"]): int(row["count"]) for row in connection.execute(
                "SELECT status,count(*) AS count FROM distribution_jobs GROUP BY status")}


def propagation_slo(queue: DistributionQueue, *, target_minutes: int = 30,
                    now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    durations: list[float] = []
    breached = 0
    with queue.connect() as connection:
        rows = connection.execute("SELECT document FROM distribution_jobs").fetchall()
    for row in rows:
        document = json.loads(row["document"])
        created = datetime.fromisoformat(str(document["created_at"]).replace("Z", "+00:00"))
        end = current
        if document.get("completed_at"):
            end = datetime.fromisoformat(str(document["completed_at"]).replace("Z", "+00:00"))
        minutes = max(0.0, (end - created).total_seconds() / 60)
        if document.get("status") == "succeeded":
            durations.append(minutes)
        elif minutes > target_minutes:
            breached += 1
    durations.sort()
    p95 = durations[max(0, math.ceil(len(durations) * .95) - 1)] if durations else None
    return {"target_minutes": target_minutes, "completed": len(durations), "breached": breached,
            "p95_minutes": round(p95, 3) if p95 is not None else None,
            "compliant": breached == 0 and (p95 is None or p95 <= target_minutes)}


def _queue(args: argparse.Namespace) -> DistributionQueue:
    return DistributionQueue(Path(args.database) if args.database else _state_dir() / "distribution.db")


def cmd_distribution_enqueue(args: argparse.Namespace) -> int:
    try:
        job = _queue(args).enqueue(args.artifact_id, args.region, account=args.account, mode=args.mode)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0


def cmd_distribution_worker(args: argparse.Namespace) -> int:
    queue = _queue(args)
    if not args.apply:
        warn(f"Dry run: queue counts {queue.counts()}; add --apply for cloud mutations")
        return 0
    worker_id = args.worker_id or f"distribution-{uuid.uuid4().hex[:10]}"
    while True:
        job = queue.claim(worker_id, global_limit=args.global_limit,
                          account_limit=args.account_limit, region_limit=args.region_limit)
        if job is None:
            if args.once:
                info("No distribution job eligible under current quotas")
                return 0
            time.sleep(args.poll_seconds)
            continue
        prefix = re.sub(r"[^A-Za-z0-9]", "_", str(job["target_account"])).upper()
        try:
            if job["mode"] == "share":
                result = share_artifact(str(job["artifact_id"]), str(job["target_account"]),
                    apply=True)
            else:
                result = execute_distribution(str(job["artifact_id"]), [str(job["target_region"])],
                    apply=True, secret_id=os.environ.get(f"{prefix}_SECRET_ID"),
                    secret_key=os.environ.get(f"{prefix}_SECRET_KEY"))
            finished = queue.finish(job, result=result, max_attempts=args.max_attempts)
        except (ConfigError, OSError, ValueError) as exc:
            finished = queue.finish(job, error=str(exc), max_attempts=args.max_attempts)
        if args.once:
            return 0 if finished["status"] == "succeeded" else 1


def cmd_distribution_slo(args: argparse.Namespace) -> int:
    result = propagation_slo(_queue(args), target_minutes=args.target_minutes)
    print(json.dumps(result, indent=2))
    return 0 if result["compliant"] else 1


def cmd_distribution_reconcile_all(args: argparse.Namespace) -> int:
    if not args.apply:
        warn("Dry run: periodic reconciliation performs no cloud reads; add --apply")
        return 0
    root = _state_dir() / "registry"
    while True:
        failed = checked = 0
        try:
            for artifact in collect_artifacts(root):
                replicas = artifact.get("replicas")
                if not isinstance(replicas, dict) or not any(
                        isinstance(item, dict) and item.get("status") == "pending"
                        for item in replicas.values()):
                    continue
                result = reconcile_distribution(str(artifact["artifact_id"]), root=root,
                                                timeout_minutes=args.timeout_minutes)
                checked += int(result["checked"])
                failed += int(result["failed"])
        except (ConfigError, OSError, ValueError) as exc:
            fail(str(exc))
            return 2
        info(f"Distribution reconcile cycle: checked={checked} failed={failed}")
        if args.once:
            return 1 if failed else 0
        time.sleep(args.interval_seconds)
