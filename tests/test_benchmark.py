from __future__ import annotations

from ohbs_image._benchmark import (
    BENCHMARK_SCHEMA,
    PHASE_BENCHMARK_SCHEMA,
    compare_benchmarks,
    phase_benchmark,
    run_benchmark,
)


def test_benchmark_contract() -> None:
    result = run_benchmark(iterations=5, warmups=1)
    assert result["schema"] == BENCHMARK_SCHEMA
    assert set(result["cases"]) == {
        "canonical_evidence_hash", "provider_protocol_verify",
        "registry_search_sqlite", "registry_upsert_sqlite",
    }
    assert all(case["median_ms"] >= 0 for case in result["cases"].values())


def test_comparison_detects_regression() -> None:
    baseline = {"cases": {"search": {"median_ms": 10.0}}}
    current = {"cases": {"search": {"median_ms": 13.0}}}
    result = compare_benchmarks(current, baseline, max_regression_percent=20)
    assert result["passed"] is False
    assert result["comparisons"][0]["change_percent"] == 30.0


def test_phase_benchmark_reports_real_p50_p95_api_and_budget_failure() -> None:
    records = [
        {"phase_duration_seconds": {"launch": value, "provision": value * 2},
         "provider_evidence": {"api_summary": {"calls": 3, "retries": 1,
                                                "failed_calls": 0}}}
        for value in (10, 20, 15)
    ]
    result = phase_benchmark(records, {"launch": 18, "provision": 45})
    assert result["schema"] == PHASE_BENCHMARK_SCHEMA
    assert result["phases"]["launch"] == {"samples": 3, "p50_seconds": 15.0,
        "p95_seconds": 20.0, "min_seconds": 10.0, "max_seconds": 20.0,
        "budget_seconds": 18, "budget_exceeded": True}
    assert result["phases"]["provision"]["budget_exceeded"] is False
    assert result["provider_api"] == {"calls": 9, "retries": 3, "failed_calls": 0}
    assert result["passed"] is False
