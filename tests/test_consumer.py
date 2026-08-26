import json
from datetime import UTC, datetime
from pathlib import Path

from ohbs_image._channels import promote_channel
from ohbs_image._consumer import resolve_admission, terraform_external_result
from ohbs_image._registry import register_release


def _artifact(tmp_path, score=98):
    release = tmp_path / "releases/img-1.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({
        "image_id": "img-1", "run_id": "run-1", "profile": "rhel10",
        "region": "ap-guangzhou", "state": "approved", "score": score,
        "attestation_signed": True,
        "approved_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    root = tmp_path / "registry"
    register_release(release, root)
    promote_channel("rhel10", "stable", "img-1", root=root)
    return root


def _policy(tmp_path, min_score=95):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "schema": "https://ohbs-image.dev/policy-bundle/v1",
        "policy_id": "production", "version": "1",
        "defaults": {"min_score": min_score, "require_attestation": True,
                     "max_age_days": 30, "max_critical_cves": 0},
        "environments": {}, "exceptions": [],
    }))
    return path


def test_consumer_resolves_channel_and_policy(tmp_path):
    root = _artifact(tmp_path)
    admission = resolve_admission("rhel10", "stable", policy_path=_policy(tmp_path),
                                  environment="production", root=root)
    assert admission["allowed"] is True
    assert admission["artifact"]["artifact_id"] == "img-1"
    assert admission["channel"]["generation"] == 1
    assert admission["policy_decision"]["environment"] == "production"


def test_consumer_returns_denied_controls(tmp_path):
    root = _artifact(tmp_path, score=80)
    admission = resolve_admission("rhel10", "stable", policy_path=_policy(tmp_path),
                                  root=root)
    assert admission["allowed"] is False
    assert admission["denied_controls"] == ["score"]


def test_terraform_result_contains_strings_only(tmp_path):
    root = _artifact(tmp_path)
    output = terraform_external_result(resolve_admission("rhel10", "stable", root=root))
    assert output["allowed"] == "true"
    assert output["image_id"] == "img-1"
    assert output["generation"] == "1"
    assert all(isinstance(value, str) for value in output.values())


def test_integration_assets_use_fail_closed_contract():
    terraform = Path(
        "integrations/terraform/modules/ohbs-image-channel/main.tf").read_text(encoding="utf-8")
    rego = Path("integrations/opa/ohbs_image_admission.rego").read_text(encoding="utf-8")
    assert '"consumer", "resolve"' in terraform
    assert 'result.allowed == "true"' in terraform
    assert 'input.allowed == true' in rego
    assert 'input.artifact.status == "active"' in rego
