from __future__ import annotations

import pytest

from ohbs_image._error_catalog import ERROR_CATALOG, error_document


def test_catalog_codes_have_stable_http_semantics() -> None:
    assert set(ERROR_CATALOG) == {
        "invalid_request", "idempotency_required", "cost_confirmation_required",
        "forbidden", "not_found", "payload_too_large", "rate_limited", "internal_error",
    }
    for code, definition in ERROR_CATALOG.items():
        document = error_document(code, "detail")
        assert document["code"] == code
        assert document["retryable"] is definition.retryable
        assert str(document["documentation"]).endswith(code)


def test_status_mismatch_fails_closed() -> None:
    with pytest.raises(ValueError, match="requires HTTP 429"):
        error_document("rate_limited", "slow down", status=400)


def test_unknown_code_cannot_leak_an_undocumented_contract() -> None:
    document = error_document("plugin_made_this_up", "detail")
    assert document["code"] == "internal_error"
    assert document["retryable"] is True
