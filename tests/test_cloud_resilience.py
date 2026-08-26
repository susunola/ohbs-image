from __future__ import annotations

import json

from ohbs_image._cloud_resilience import CircuitBreaker, classify_provider_error, record_takeover
from ohbs_image._failures import FailureCategory


def test_circuit_opens_then_allows_half_open_probe():
    breaker = CircuitBreaker(threshold=2, reset_seconds=10)
    breaker.failure("cvm.RunInstances", now=100)
    assert breaker.allow("cvm.RunInstances", now=101)
    breaker.failure("cvm.RunInstances", now=102)
    assert not breaker.allow("cvm.RunInstances", now=105)
    assert breaker.allow("cvm.RunInstances", now=112)
    breaker.success("cvm.RunInstances")
    assert breaker.allow("cvm.RunInstances", now=113)


def test_capacity_errors_delegate_to_fallback_without_same_placement_retry():
    failure = classify_provider_error(
        "ResourceInsufficient.SpecifiedInstanceType", "instance sold out")
    assert failure.category == FailureCategory.CAPACITY
    assert failure.retryable is False


def test_terminal_failure_log_is_secret_free_and_actionable(tmp_path):
    path = tmp_path / "takeover.jsonl"
    failure = classify_provider_error("InternalError", "request failed")
    record_takeover(path, operation="cvm.RunInstances", failure=failure, attempts=3)
    event = json.loads(path.read_text())
    assert event["requires_manual_takeover"] is True
    assert event["failure"]["retryable"] is True
    assert "secret" not in path.read_text().lower()
