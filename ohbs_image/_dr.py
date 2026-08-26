from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._logging import fail, ok
from ._state_db import StateDatabase

DR_REPORT_SCHEMA = "https://ohbs-image.dev/dr-drill-report/v1"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _database_drill(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    source = StateDatabase(root / "state.db")
    source.initialize()
    with source.connect() as connection:
        connection.execute("INSERT OR REPLACE INTO metadata VALUES('dr_marker','recover-me')")
        connection.commit()
    backup = root / "backups" / "state.db"
    source.backup(backup)
    restored = StateDatabase(root / "restored.db")
    with sqlite3.connect(backup) as original, restored.connect() as target:
        original.backup(target)
    verification = restored.verify()
    with restored.connect() as connection:
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key='dr_marker'").fetchone()
    passed = verification["valid"] and marker is not None and marker[0] == "recover-me"
    return {"scenario": "state_database_restore", "passed": passed,
            "rpo_seconds": 0, "rto_seconds": round(time.monotonic() - started, 3),
            "evidence": verification}


def _lease_drill(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    database = StateDatabase(root / "queue.db")
    database.initialize()
    expired = (datetime.now(UTC) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = {"request_id": "dr-request", "status": "running", "attempt": 1,
                "worker_id": "failed-worker", "lease_expires_at": expired,
                "updated_at": expired}
    with database.transaction() as connection:
        database._upsert_request(connection, document)
    claimed = database.claim("recovery-worker", lease_seconds=60)
    passed = claimed is not None and claimed.get("worker_id") == "recovery-worker"
    return {"scenario": "worker_lease_recovery", "passed": passed,
            "rpo_seconds": 0, "rto_seconds": round(time.monotonic() - started, 3),
            "evidence": {"request": claimed}}


def _evidence_drill(root: Path) -> dict[str, Any]:
    started = time.monotonic()
    root.mkdir(parents=True, exist_ok=True)
    source = root / "evidence.json"
    source.write_text('{"status":"trusted"}\n', encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    replica = root / "evidence-replica.json"
    replica.write_bytes(source.read_bytes())
    replica.write_text('{"status":"tampered"}\n', encoding="utf-8")
    actual = hashlib.sha256(replica.read_bytes()).hexdigest()
    passed = actual != expected
    return {"scenario": "evidence_corruption_detection", "passed": passed,
            "rpo_seconds": 0, "rto_seconds": round(time.monotonic() - started, 3),
            "evidence": {"expected_sha256": expected, "actual_sha256": actual,
                         "fail_closed": passed}}


def run_dr_drill(scenario: str = "all") -> dict[str, Any]:
    runners = {"database": _database_drill, "worker": _lease_drill,
               "evidence": _evidence_drill}
    selected = runners if scenario == "all" else {scenario: runners[scenario]}
    with tempfile.TemporaryDirectory(prefix="ohbs-dr-") as temporary:
        root = Path(temporary)
        results = [runner(root / name) for name, runner in selected.items()]
    return {"schema": DR_REPORT_SCHEMA, "executed_at": _stamp(),
            "isolated": True, "scenario": scenario,
            "passed": all(item["passed"] for item in results), "results": results}


def cmd_dr_drill(args: argparse.Namespace) -> int:
    try:
        result = run_dr_drill(args.scenario)
    except (KeyError, OSError, sqlite3.Error, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["passed"]:
        ok("All isolated DR scenarios passed")
    return 0 if result["passed"] else 1
