from __future__ import annotations

import argparse
import html
import json
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ._config import _state_dir
from ._metrics import collect_metrics
from ._registry import _hash
from ._reports import _state_lock
from ._state_db import StateDatabase

PROOF_SCHEMA = "https://ohbs-image.dev/production-proof/v1"
PROOF_REPORT_SCHEMA = "https://ohbs-image.dev/production-proof-report/v1"


def scale_recovery_benchmark(size: int = 1000) -> dict[str, Any]:
    if not 10 <= size <= 100_000:
        raise ValueError("benchmark size must be between 10 and 100000")
    with tempfile.TemporaryDirectory(prefix="ohbs-proof-") as directory:
        root = Path(directory)
        database = StateDatabase(root / "state.db")
        started = time.perf_counter()
        for index in range(size):
            document = {"artifact_id": f"proof-{index:06d}", "bucket": f"bucket-{index % 10}",
                        "version": str(index), "status": "active",
                        "created_at": "2026-01-01T00:00:00Z"}
            document["document_hash"] = _hash(document)
            database.upsert_artifact(document)
        ingest_seconds = time.perf_counter() - started
        started = time.perf_counter()
        count, rows = database.search_artifacts(query="proof-", limit=1000)
        search_seconds = time.perf_counter() - started
        backup = root / "backup.db"
        started = time.perf_counter()
        database.backup(backup)
        backup_seconds = time.perf_counter() - started
        verification = StateDatabase(backup).verify()
    return {"dataset": "synthetic-artifact-registry/v1", "size": size,
            "ingest_seconds": round(ingest_seconds, 4),
            "ingest_per_second": round(size / max(ingest_seconds, .000001), 2),
            "search_seconds": round(search_seconds, 6), "search_count": count,
            "search_page_count": len(rows), "backup_seconds": round(backup_seconds, 4),
            "recovery_verified": bool(verification["valid"])}


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid proof ledger line {number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid proof ledger line {number}")
        rows.append(value)
    return rows


def verify_ledger(rows: list[dict[str, Any]]) -> dict[str, Any]:
    failures = []
    previous = ""
    dates = set()
    for index, row in enumerate(rows):
        if row.get("previous_hash") != previous:
            failures.append(f"entry {index} previous_hash mismatch")
        if row.get("document_hash") != _hash(row):
            failures.append(f"entry {index} document_hash mismatch")
        day = str(row.get("date") or "")
        if day in dates:
            failures.append(f"duplicate date: {day}")
        dates.add(day)
        previous = str(row.get("document_hash") or "")
    return {"valid": not failures, "entries": len(rows), "failures": failures,
            "head": previous}


def record_daily_proof(path: Path, root: Path, *, day: date | None = None,
                       benchmark_size: int = 1000) -> dict[str, Any]:
    rows = _read_ledger(path)
    verification = verify_ledger(rows)
    if not verification["valid"]:
        raise ValueError("proof ledger failed verification")
    actual_day = (day or datetime.now(UTC).date()).isoformat()
    if any(row.get("date") == actual_day for row in rows):
        raise ValueError(f"proof already recorded for {actual_day}")
    entry: dict[str, Any] = {"schema": PROOF_SCHEMA, "date": actual_day,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metrics": collect_metrics(root, days=1),
        "scale_recovery": scale_recovery_benchmark(benchmark_size),
        "previous_hash": verification["head"]}
    entry["document_hash"] = _hash(entry)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _state_lock(path)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
    finally:
        lock.rmdir()
    return entry


def proof_report(rows: list[dict[str, Any]], *, window_days: int = 30) -> dict[str, Any]:
    verification = verify_ledger(rows)
    cutoff = datetime.now(UTC).date() - timedelta(days=window_days - 1)
    selected = [row for row in rows if date.fromisoformat(str(row["date"])) >= cutoff]
    terminal = sum(int(row["metrics"]["runs"]["successful"]) +
                   int(row["metrics"]["runs"]["failed"]) for row in selected)
    successful = sum(int(row["metrics"]["runs"]["successful"]) for row in selected)
    success_rate = round(100 * successful / terminal, 2) if terminal else None
    recovery_passes = sum(bool(row["scale_recovery"]["recovery_verified"]) for row in selected)
    distinct_days = len({row["date"] for row in selected})
    claims = {"coverage_complete": distinct_days >= window_days,
              "success_rate_target": success_rate is not None and success_rate >= 98,
              "recovery_verified_daily": recovery_passes == distinct_days and distinct_days > 0}
    return {"schema": PROOF_REPORT_SCHEMA, "window_days": window_days,
            "coverage_days": distinct_days, "ledger_valid": verification["valid"],
            "terminal_runs": terminal, "success_rate": success_rate,
            "recovery_passes": recovery_passes, "claims": claims,
            "production_proof_complete": verification["valid"] and all(claims.values()),
            "entries": selected}


def render_proof_html(report: dict[str, Any]) -> str:
    status = "VERIFIED" if report["production_proof_complete"] else "EVIDENCE INCOMPLETE"
    rows = "".join(f"<tr><td>{html.escape(str(row['date']))}</td><td>{row['metrics']['runs']['successful']}</td>"
                   f"<td>{row['metrics']['runs']['failed']}</td><td>{row['scale_recovery']['size']}</td>"
                   f"<td>{'pass' if row['scale_recovery']['recovery_verified'] else 'fail'}</td></tr>"
                   for row in report["entries"])
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>ohbs-image production proof</title><style>:root{{--ink:#17202b;--line:#d8dee6;--ok:#18734d;--warn:#946113}}body{{font:15px/1.5 system-ui;color:var(--ink);max-width:1050px;margin:auto;padding:32px}}h1{{font-size:42px}}.status{{display:inline-block;padding:8px 12px;border:2px solid var(--warn);font-weight:800}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left}}@media print{{body{{padding:0}}}}</style></head><body><p>ohbs-image / production evidence</p><h1>{status}</h1><p>{report['coverage_days']} of {report['window_days']} required days · success rate {report['success_rate']} · ledger valid {report['ledger_valid']}</p><table><thead><tr><th>Date</th><th>Success</th><th>Failed</th><th>Scale</th><th>Recovery</th></tr></thead><tbody>{rows}</tbody></table><footer>Generated {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} · claims remain incomplete until every threshold is met.</footer></body></html>"""


def _ledger(args: argparse.Namespace) -> Path:
    return Path(args.ledger) if args.ledger else _state_dir() / "proof" / "daily.jsonl"


def cmd_proof_record(args: argparse.Namespace) -> int:
    entry = record_daily_proof(_ledger(args), _state_dir(), benchmark_size=args.size)
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


def cmd_proof_verify(args: argparse.Namespace) -> int:
    result = verify_ledger(_read_ledger(_ledger(args)))
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


def cmd_proof_report(args: argparse.Namespace) -> int:
    report = proof_report(_read_ledger(_ledger(args)), window_days=args.days)
    if args.html:
        Path(args.html).write_text(render_proof_html(report), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["production_proof_complete"] else 3
