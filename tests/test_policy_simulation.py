from __future__ import annotations

from datetime import UTC, datetime

from ohbs_image._policy import POLICY_SCHEMA
from ohbs_image._policy_simulation import POLICY_SIMULATION_SCHEMA, simulate_policy


def policy(score: int, version: str) -> dict:
    return {"schema": POLICY_SCHEMA, "policy_id": "production", "version": version,
            "defaults": {"allowed_status": ["active"], "min_score": score},
            "environments": {}, "exceptions": []}


def test_simulation_reports_transitions_without_mutation() -> None:
    artifacts = [
        {"artifact_id": "good", "bucket": "linux", "version": "1", "status": "active",
         "score": 95, "created_at": "2026-01-01T00:00:00Z"},
        {"artifact_id": "borderline", "bucket": "linux", "version": "2", "status": "active",
         "score": 85, "created_at": "2026-01-01T00:00:00Z"},
    ]
    result = simulate_policy(policy(90, "2"), artifacts, "production",
                             baseline=policy(80, "1"),
                             now=datetime(2026, 1, 2, tzinfo=UTC))
    assert result["schema"] == POLICY_SIMULATION_SCHEMA
    assert result["dry_run"] is True
    assert result["summary"] == {"allowed": 1, "denied": 1, "newly_allowed": 0,
                                 "newly_denied": 1, "unchanged": 1}
    assert result["control_denials"] == {"score": 1}
    assert artifacts[1]["score"] == 85
