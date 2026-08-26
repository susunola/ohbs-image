"""Cross-module acceptance journeys expressed in the language of real users."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ohbs_image._channels import promote_channel, resolve_channel
from ohbs_image._consumer import resolve_admission, terraform_external_result
from ohbs_image._distribution import distribution_plan
from ohbs_image._policy import POLICY_SCHEMA
from ohbs_image._policy_simulation import simulate_policy
from ohbs_image._registry import change_artifact_status, get_artifact, register_release


def _register(root, image_id: str, *, score: int) -> None:
    release = root / "releases" / f"{image_id}.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(json.dumps({
        "image_id": image_id,
        "run_id": f"run-{image_id}",
        "profile": "rhel10",
        "region": "ap-guangzhou",
        "state": "approved",
        "score": score,
        "attestation_signed": True,
        "approved_at": "2026-08-26T00:00:00Z",
    }), encoding="utf-8")
    register_release(release, root / "registry")


def _policy(path, score: int) -> None:
    path.write_text(json.dumps({
        "schema": POLICY_SCHEMA,
        "policy_id": "production",
        "version": "1",
        "defaults": {
            "allowed_status": ["active"],
            "min_score": score,
            "require_attestation": True,
            "max_age_days": 365,
        },
        "environments": {},
        "exceptions": [],
    }), encoding="utf-8")


def test_platform_engineer_journey_registers_promotes_and_plans_distribution(tmp_path) -> None:
    root = tmp_path / "state"
    _register(root, "img-v1", score=96)
    pointer = promote_channel("rhel10", "stable", "img-v1", expected_generation=0,
                              root=root / "registry")
    plan = distribution_plan("img-v1", ["ap-guangzhou", "ap-shanghai"], root / "registry")
    assert pointer["generation"] == 1
    assert plan["copy_count"] == 1
    assert [(row["region"], row["action"]) for row in plan["actions"]] == [
        ("ap-guangzhou", "skip"), ("ap-shanghai", "copy"),
    ]


def test_security_journey_simulates_then_revokes_with_automatic_rollback(tmp_path) -> None:
    root = tmp_path / "state"
    _register(root, "img-v1", score=96)
    _register(root, "img-v2", score=89)
    promote_channel("rhel10", "stable", "img-v1", root=root / "registry")
    promote_channel("rhel10", "stable", "img-v2", root=root / "registry")
    artifacts = [get_artifact(image_id, root / "registry") for image_id in ("img-v1", "img-v2")]
    candidate = {
        "schema": POLICY_SCHEMA,
        "policy_id": "production",
        "version": "2",
        "defaults": {"allowed_status": ["active"], "min_score": 95},
        "environments": {},
        "exceptions": [],
    }
    simulation = simulate_policy(candidate, artifacts, "production",
                                 now=datetime(2026, 8, 26, tzinfo=UTC))
    assert simulation["summary"]["denied"] == 1
    result = change_artifact_status(
        "img-v2", "revoked", actor="security", reason="policy regression",
        root=root / "registry",
    )
    assert result["channel_rollbacks"][0]["to"] == "img-v1"
    assert resolve_channel("rhel10", "stable", root / "registry")["artifact"]["artifact_id"] == "img-v1"


def test_consumer_journey_allows_verified_image_and_fails_closed_after_quarantine(tmp_path) -> None:
    root = tmp_path / "state"
    _register(root, "img-v1", score=96)
    promote_channel("rhel10", "stable", "img-v1", root=root / "registry")
    policy = tmp_path / "policy.json"
    _policy(policy, 95)
    admission = resolve_admission(
        "rhel10", "stable", policy_path=policy, environment="production", root=root / "registry",
    )
    external = terraform_external_result(admission)
    assert external["allowed"] == "true"
    assert external["image_id"] == "img-v1"
    change_artifact_status(
        "img-v1", "quarantined", actor="security", reason="investigation",
        auto_rollback=False, root=root / "registry",
    )
    with pytest.raises(ValueError, match="not active"):
        resolve_admission(
            "rhel10", "stable", policy_path=policy, environment="production",
            root=root / "registry",
        )
