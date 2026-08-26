import json
from datetime import UTC, datetime

from ohbs_image._policy import evaluate_policy, load_policy, verify_policy


def _policy(**defaults):
    return {"schema": "https://ohbs-image.dev/policy-bundle/v1",
            "policy_id": "organization", "version": "1", "defaults": defaults,
            "environments": {}, "exceptions": []}


def _artifact(**overrides):
    value = {"artifact_id": "img-1", "status": "active", "score": 98,
             "attestation_signed": True, "created_at": "2026-08-20T00:00:00Z",
             "critical_cves": 0}
    value.update(overrides)
    return value


def test_policy_allows_compliant_artifact():
    decision = evaluate_policy(
        _policy(min_score=95, require_attestation=True, max_age_days=30,
                max_critical_cves=0), _artifact(), "production",
        now=datetime(2026, 8, 26, tzinfo=UTC))
    assert decision["allowed"] is True
    assert {item["result"] for item in decision["checks"]} == {"pass"}


def test_environment_override_denies_low_score():
    policy = _policy(min_score=80)
    policy["environments"] = {"production": {"min_score": 99}}
    decision = evaluate_policy(policy, _artifact(score=98), "production",
                               now=datetime(2026, 8, 26, tzinfo=UTC))
    assert decision["allowed"] is False
    assert next(item for item in decision["checks"] if item["control"] == "score")["result"] == "deny"


def test_zero_min_score_disables_score_gate():
    decision = evaluate_policy(_policy(min_score=0), _artifact(score=None), "development",
                               now=datetime(2026, 8, 26, tzinfo=UTC))
    assert next(item for item in decision["checks"] if item["control"] == "score")["result"] == "pass"


def test_approved_unexpired_exception_waives_one_control():
    policy = _policy(min_score=99)
    policy["exceptions"] = [{"id": "EX-12", "artifact_id": "img-1",
                             "environment": "production", "controls": ["score"],
                             "owner": "platform", "approved_by": "security",
                             "reason": "emergency patch", "expires_at": "2026-08-27T00:00:00Z"}]
    assert verify_policy(policy) == []
    decision = evaluate_policy(policy, _artifact(score=98), "production",
                               now=datetime(2026, 8, 26, tzinfo=UTC))
    score = next(item for item in decision["checks"] if item["control"] == "score")
    assert decision["allowed"] is True
    assert score["result"] == "exception" and score["exception_id"] == "EX-12"


def test_expired_exception_does_not_waive_control():
    policy = _policy(require_attestation=True)
    policy["exceptions"] = [{"id": "EX-OLD", "controls": ["attestation"],
                             "owner": "platform", "approved_by": "security",
                             "reason": "expired", "expires_at": "2026-08-25T00:00:00Z"}]
    decision = evaluate_policy(policy, _artifact(attestation_signed=False), "production",
                               now=datetime(2026, 8, 26, tzinfo=UTC))
    assert decision["allowed"] is False


def test_policy_inheritance_merges_defaults_environments_and_exceptions(tmp_path):
    parent = _policy(min_score=90, require_attestation=True)
    parent["environments"] = {"production": {"max_age_days": 30}}
    (tmp_path / "parent.json").write_text(json.dumps(parent))
    child = {"schema": parent["schema"], "policy_id": "team-a", "version": "2",
             "extends": "parent.json", "defaults": {"min_score": 95},
             "environments": {"production": {"max_critical_cves": 0}}, "exceptions": []}
    (tmp_path / "child.json").write_text(json.dumps(child))
    loaded = load_policy(tmp_path / "child.json")
    assert loaded["defaults"] == {"min_score": 95, "require_attestation": True}
    assert loaded["environments"]["production"] == {"max_age_days": 30,
                                                       "max_critical_cves": 0}
