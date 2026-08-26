from __future__ import annotations

from datetime import UTC, datetime

from ohbs_image._policy import POLICY_SCHEMA
from ohbs_image._policy_lifecycle import diff_policy, preview_exceptions


def bundle(version: str, score: int = 80) -> dict:
    return {"schema": POLICY_SCHEMA, "policy_id": "prod", "version": version,
            "defaults": {"allowed_status": ["active"], "min_score": score},
            "environments": {}, "exceptions": []}


def test_policy_diff_is_field_level() -> None:
    result = diff_policy(bundle("2", 90), bundle("1", 80))
    paths = {change["path"] for change in result["changes"]}
    assert {"version", "defaults.min_score"} <= paths


def test_exception_preview_shows_revocation_impact() -> None:
    policy = bundle("1", 90)
    policy["exceptions"] = [{"id": "temporary-score", "owner": "platform",
        "approved_by": "security", "reason": "migration",
        "expires_at": "2026-02-01T00:00:00Z", "controls": ["score"]}]
    artifacts = [{"artifact_id": "img-low", "status": "active", "score": 85,
                  "created_at": "2026-01-01T00:00:00Z"}]
    result = preview_exceptions(policy, artifacts, "production",
                                now=datetime(2026, 1, 15, tzinfo=UTC))
    row = result["exceptions"][0]
    assert row["status"] == "expiring"
    assert row["affected_artifacts"] == ["img-low"]
