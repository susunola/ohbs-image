from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import pytest

from ohbs_image._policy import (
    POLICY_EXCEPTIONS_SCHEMA,
    POLICY_SCHEMA,
    cmd_policy_exceptions,
    policy_exceptions,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _exception(identifier: str, expires_at: str, environment: str = "production",
               artifact_id: str | None = None) -> dict:
    item = {
        "id": identifier,
        "controls": ["freshness"],
        "owner": "platform-team",
        "approved_by": "security-team",
        "reason": "migration window",
        "environment": environment,
        "expires_at": expires_at,
    }
    if artifact_id is not None:
        item["artifact_id"] = artifact_id
    return item


def _policy(*exceptions: dict) -> dict:
    return {
        "schema": POLICY_SCHEMA,
        "policy_id": "organization-golden-images",
        "version": "4",
        "defaults": {"min_score": 90},
        "environments": {"production": {"min_score": 95}},
        "exceptions": list(exceptions),
    }


def test_expiry_states_are_classified_against_the_horizon() -> None:
    policy = _policy(
        _exception("expired-one", "2026-08-01T00:00:00Z"),
        _exception("near-one", "2026-09-05T00:00:00Z"),
        _exception("far-one", "2027-01-01T00:00:00Z"),
    )
    # No horizon: anything unexpired is simply active.
    plain = policy_exceptions(policy, now=NOW)
    assert plain["summary"] == {"active": 2, "expiring": 0, "expired": 1, "total": 3}
    # A 7-day horizon surfaces the one lapsing on 2026-09-05.
    horizon = policy_exceptions(policy, within_days=7, now=NOW)
    states = {row["id"]: row["status"] for row in horizon["exceptions"]}
    assert states == {"expired-one": "expired", "near-one": "expiring", "far-one": "active"}
    assert horizon["summary"]["expiring"] == 1
    assert horizon["schema"] == POLICY_EXCEPTIONS_SCHEMA
    assert len(horizon["document_hash"]) == 64


def test_remaining_days_track_the_evaluation_instant() -> None:
    policy = _policy(_exception("near-one", "2026-09-05T00:00:00Z"))
    row = policy_exceptions(policy, now=NOW)["exceptions"][0]
    assert row["remaining_days"] == 4
    later = policy_exceptions(policy, now=datetime(2026, 9, 4, tzinfo=UTC))
    assert later["exceptions"][0]["remaining_days"] == 1


def test_environment_filter_keeps_only_matching_exceptions() -> None:
    policy = _policy(
        _exception("prod-one", "2026-10-01T00:00:00Z", environment="production"),
        _exception("dev-one", "2026-10-01T00:00:00Z", environment="development"),
    )
    all_rows = policy_exceptions(policy, now=NOW)
    assert {row["id"] for row in all_rows["exceptions"]} == {"prod-one", "dev-one"}
    filtered = policy_exceptions(policy, "development", now=NOW)
    assert [row["id"] for row in filtered["exceptions"]] == ["dev-one"]
    assert filtered["environment"] == "development"
    assert filtered["summary"]["total"] == 1


def test_rows_are_ordered_by_expiry_then_id() -> None:
    policy = _policy(
        _exception("later", "2026-12-01T00:00:00Z"),
        _exception("sooner", "2026-10-01T00:00:00Z"),
        _exception("same-day", "2026-10-01T00:00:00Z"),
    )
    order = [row["id"] for row in policy_exceptions(policy, now=NOW)["exceptions"]]
    assert order == ["same-day", "sooner", "later"]


def test_policy_exceptions_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="invalid policy"):
        policy_exceptions({"schema": "nope"}, now=NOW)
    with pytest.raises(ValueError, match="within_days"):
        policy_exceptions(_policy(), within_days=-1, now=NOW)


def test_policy_exceptions_cli_reports_json_and_exit_codes(tmp_path, capsys) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy(_exception("gone", "2026-08-01T00:00:00Z"))),
                    encoding="utf-8")

    clean = argparse.Namespace(bundle=str(path), environment="", within_days=0,
                               fail_on_expired=False, output="json")
    assert cmd_policy_exceptions(clean) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == POLICY_EXCEPTIONS_SCHEMA

    strict = argparse.Namespace(bundle=str(path), environment="", within_days=0,
                                fail_on_expired=True, output="json")
    assert cmd_policy_exceptions(strict) == 1
    capsys.readouterr()

    text = argparse.Namespace(bundle=str(path), environment="production", within_days=30,
                              fail_on_expired=False, output="text")
    assert cmd_policy_exceptions(text) == 0
    out = capsys.readouterr().out
    assert "expired" in out
    assert "within 30d" in out


def test_policy_exceptions_cli_fails_closed_on_bad_bundle(tmp_path, caplog) -> None:
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
    args = argparse.Namespace(bundle=str(path), environment="", within_days=0,
                              fail_on_expired=False, output="json")
    with caplog.at_level("ERROR"):
        assert cmd_policy_exceptions(args) == 2
    assert "invalid policy" in caplog.text
