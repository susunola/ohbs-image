from ohbs_image._failures import FailureCategory, classify_failure, retry_delay


def test_classifies_retryable_transient_failures():
    for message, category in (
        ("HTTP 429 too many requests", FailureCategory.RATE_LIMIT),
        ("resource sold out: no stock", FailureCategory.CAPACITY),
        ("connection reset by peer", FailureCategory.NETWORK),
        ("deadline exceeded", FailureCategory.TIMEOUT),
    ):
        failure = classify_failure(message, phase="packer-build")
        assert failure.category is category
        assert failure.retryable is True
        assert failure.phase == "packer-build"


def test_security_policy_and_configuration_fail_fast():
    for message in ("clean-boot verification FAILED", "policy denied release",
                    "invalid config syntax error", "credential is invalid"):
        assert classify_failure(message).retryable is False


def test_unknown_failure_is_not_retried():
    failure = classify_failure("something novel happened")
    assert failure.category is FailureCategory.UNKNOWN
    assert failure.retryable is False


def test_retry_delay_is_capped_exponential():
    assert [retry_delay(i, maximum_seconds=5) for i in range(1, 6)] == [1, 2, 4, 5, 5]
