from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._providers import load_providers, verify_provider
from ._state_db import StateDatabase

BENCHMARK_SCHEMA = "https://ohbs-image.dev/benchmark/v1"
PHASE_BENCHMARK_SCHEMA = "https://ohbs-image.dev/native-phase-benchmark/v1"


def _measure(operation: Callable[[int], None], iterations: int, warmups: int) -> dict[str, Any]:
    for index in range(warmups):
        operation(index)
    samples: list[float] = []
    for index in range(iterations):
        started = time.perf_counter_ns()
        operation(index)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "iterations": iterations,
        "mean_ms": round(statistics.fmean(samples), 6),
        "median_ms": round(statistics.median(samples), 6),
        "p95_ms": round(ordered[p95_index], 6),
        "ops_per_second": round(1000 / statistics.fmean(samples), 3),
    }


def run_benchmark(*, iterations: int = 100, warmups: int = 10) -> dict[str, Any]:
    if iterations < 5 or iterations > 100_000:
        raise ValueError("iterations must be between 5 and 100000")
    if warmups < 0 or warmups > iterations:
        raise ValueError("warmups must be between 0 and iterations")
    fixture = {
        "artifact_id": "benchmark-image",
        "bucket": "stable",
        "version": "1.0.0",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
        "labels": {"profile": "ubuntu22", "level": "2"},
    }
    fixture["document_hash"] = hashlib.sha256(
        json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    with tempfile.TemporaryDirectory(prefix="ohbs-image-benchmark-") as directory:
        database = StateDatabase(Path(directory) / "state.db")
        database.initialize()

        def canonicalize(index: int) -> None:
            payload = {**fixture, "sequence": index}
            hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()

        def upsert(index: int) -> None:
            document = {**fixture, "artifact_id": f"bench-{index % 25}"}
            document["document_hash"] = hashlib.sha256(str(index).encode()).hexdigest()
            database.upsert_artifact(document)

        for index in range(100):
            upsert(index)

        def search(index: int) -> None:
            database.search_artifacts(query=f"bench-{index % 25}", limit=10)

        provider = load_providers(include_external=False)["tencentcloud"]

        def protocol_check(_index: int) -> None:
            verify_provider(provider)

        cases = {
            "canonical_evidence_hash": _measure(canonicalize, iterations, warmups),
            "registry_upsert_sqlite": _measure(upsert, iterations, warmups),
            "registry_search_sqlite": _measure(search, iterations, warmups),
            "provider_protocol_verify": _measure(protocol_check, iterations, warmups),
        }

    return {
        "schema": BENCHMARK_SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "methodology": {"clock": "perf_counter_ns", "iterations": iterations, "warmups": warmups},
        "cases": cases,
    }


def compare_benchmarks(current: dict[str, Any], baseline: dict[str, Any],
                       *, max_regression_percent: float = 20.0) -> dict[str, Any]:
    comparisons = []
    failed = False
    for name, result in current.get("cases", {}).items():
        old = baseline.get("cases", {}).get(name)
        if not isinstance(old, dict) or not isinstance(result, dict):
            continue
        before = float(old["median_ms"])
        after = float(result["median_ms"])
        change = 0.0 if before == 0 else ((after - before) / before) * 100
        regressed = change > max_regression_percent
        failed = failed or regressed
        comparisons.append({"case": name, "baseline_ms": before, "current_ms": after,
                            "change_percent": round(change, 2), "regressed": regressed})
    return {"passed": not failed, "max_regression_percent": max_regression_percent,
            "comparisons": comparisons}


def phase_benchmark(records: list[dict[str, Any]],
                    budgets: dict[str, float] | None = None) -> dict[str, Any]:
    """Aggregate real native build records into phase P50/P95 release evidence."""
    samples: dict[str, list[float]] = {}
    for record in records:
        for phase, raw in (record.get("phase_duration_seconds") or {}).items():
            samples.setdefault(str(phase), []).append(max(0.0, float(raw)))
    phases: dict[str, dict[str, Any]] = {}
    failed = False
    for phase, values in sorted(samples.items()):
        ordered = sorted(values)
        p95 = ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]
        budget = (budgets or {}).get(phase)
        exceeded = budget is not None and p95 > float(budget)
        failed = failed or exceeded
        phases[phase] = {"samples": len(values),
                         "p50_seconds": round(statistics.median(values), 3),
                         "p95_seconds": round(p95, 3),
                         "min_seconds": round(ordered[0], 3),
                         "max_seconds": round(ordered[-1], 3),
                         "budget_seconds": budget, "budget_exceeded": exceeded}
    api_calls = [((record.get("provider_evidence") or {}).get("api_summary") or {})
                 for record in records]
    return {"schema": PHASE_BENCHMARK_SCHEMA, "records": len(records),
            "passed": not failed, "phases": phases,
            "provider_api": {"calls": sum(int(row.get("calls") or 0) for row in api_calls),
                             "retries": sum(int(row.get("retries") or 0) for row in api_calls),
                             "failed_calls": sum(int(row.get("failed_calls") or 0)
                                                 for row in api_calls)}}


def cmd_benchmark_run(args: argparse.Namespace) -> int:
    document = run_benchmark(iterations=args.iterations, warmups=args.warmups)
    rendered = json.dumps(document, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def cmd_benchmark_compare(args: argparse.Namespace) -> int:
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    result = compare_benchmarks(current, baseline,
                                max_regression_percent=args.max_regression_percent)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


def cmd_benchmark_phases(args: argparse.Namespace) -> int:
    records = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.record]
    budgets = json.loads(Path(args.budgets).read_text(encoding="utf-8")) if args.budgets else {}
    result = phase_benchmark(records, budgets)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1
