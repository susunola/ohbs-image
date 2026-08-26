from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._run_events import read_run_events

SLO_SCHEMA = "https://ohbs-image.dev/run-slo/v1"
_SUCCESS = {"APPROVED", "DISTRIBUTED"}
_FAILURE = {"FAILED", "TIMED_OUT", "CANCELLED"}


def _timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def calculate_run_slo(root: Path, *, days: int = 30) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    totals = {"successful": 0, "failed": 0, "active": 0, "retried": 0}
    durations: list[float] = []
    categories: dict[str, int] = {}
    runs = 0
    for path in sorted((root / "events").glob("*.jsonl")):
        events = read_run_events(path.stem, root)
        if not events:
            continue
        started = _timestamp(events[0].get("timestamp"))
        if started is None or started < cutoff:
            continue
        runs += 1
        states = [str(event.get("to") or "") for event in events]
        final = states[-1]
        if final in _SUCCESS:
            totals["successful"] += 1
        elif final in _FAILURE:
            totals["failed"] += 1
        else:
            totals["active"] += 1
        if "RETRYING" in states:
            totals["retried"] += 1
        ended = _timestamp(events[-1].get("timestamp"))
        if ended is not None and final in (_SUCCESS | _FAILURE):
            durations.append(max(0.0, (ended - started).total_seconds()))
        if final in _FAILURE:
            metadata = events[-1].get("metadata")
            category = str(metadata.get("failure_category") or "unknown") \
                if isinstance(metadata, dict) else "unknown"
            categories[category] = categories.get(category, 0) + 1
    terminal = totals["successful"] + totals["failed"]
    return {
        "schema": SLO_SCHEMA,
        "window_days": max(1, days),
        "runs": runs,
        **totals,
        "success_rate": round(100 * totals["successful"] / terminal, 2) if terminal else None,
        "retry_rate": round(100 * totals["retried"] / runs, 2) if runs else None,
        "duration_seconds": {
            "p50": _percentile(durations, .50),
            "p95": _percentile(durations, .95),
            "max": round(max(durations), 2) if durations else None,
        },
        "failure_categories": dict(sorted(categories.items())),
    }


def cmd_report_slo(args: argparse.Namespace) -> int:
    doc = calculate_run_slo(_lineage_path().parent, days=args.days)
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    rate = "n/a" if doc["success_rate"] is None else f"{doc['success_rate']}%"
    retry = "n/a" if doc["retry_rate"] is None else f"{doc['retry_rate']}%"
    print(f"Run SLO ({doc['window_days']}d): {doc['runs']} run(s)")
    print(f"  success rate  {rate}")
    print(f"  retry rate    {retry}")
    print(f"  duration p50  {doc['duration_seconds']['p50'] or 'n/a'}s")
    print(f"  duration p95  {doc['duration_seconds']['p95'] or 'n/a'}s")
    return 0
