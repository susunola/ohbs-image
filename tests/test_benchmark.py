from __future__ import annotations

from ohbs_image._benchmark import BENCHMARK_SCHEMA, compare_benchmarks, run_benchmark


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
