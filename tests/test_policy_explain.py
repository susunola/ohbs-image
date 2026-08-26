from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from ohbs_image._policy import POLICY_SCHEMA, cmd_policy_explain, explain_policy


def _policy() -> dict:
    return {
        "schema": POLICY_SCHEMA,
        "policy_id": "production",
        "version": "3",
        "defaults": {"min_score": 85, "require_attestation": True},
        "environments": {"production": {"min_score": 95}},
        "exceptions": [{
            "id": "temporary-score",
            "artifact_id": "img-1",
            "environment": "production",
            "controls": ["score"],
            "owner": "platform",
            "approved_by": "security",
            "reason": "migration",
            "expires_at": "2026-09-01T00:00:00Z",
        }],
    }


def test_explanation_tracks_sources_and_applicable_exceptions() -> None:
    result = explain_policy(
        _policy(), "production", artifact_id="img-1",
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )
    controls = {item["control"]: item for item in result["controls"]}
    assert controls["min_score"] == {
        "control": "min_score", "value": 95, "source": "environments.production",
    }
    assert controls["require_attestation"]["source"] == "defaults"
    assert result["exceptions"][0]["status"] == "active"
    assert result["exceptions"][0]["applicable"] is True
    assert len(result["document_hash"]) == 64


def test_explanation_distinguishes_expired_and_not_applicable() -> None:
    expired = explain_policy(
        _policy(), "production", artifact_id="img-1",
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert expired["exceptions"][0]["status"] == "expired"
    other = explain_policy(
        _policy(), "production", artifact_id="img-other",
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )
    assert other["exceptions"][0]["status"] == "not_applicable"


def test_policy_explain_cli_outputs_machine_contract(tmp_path, capsys) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    args = argparse.Namespace(
        bundle=str(path), environment="production", artifact_id="img-1", output="json",
    )
    assert cmd_policy_explain(args) == 0
    assert json.loads(capsys.readouterr().out)["schema"].endswith("policy-explanation/v1")
