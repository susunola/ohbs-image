import json

import pytest

from ohbs_image._channels import ChannelConflictError, collect_channels, promote_channel, resolve_channel
from ohbs_image._registry import change_artifact_status, register_release


def _artifact(tmp_path, image_id="img-1", profile="rhel10", state="approved"):
    release = tmp_path / "releases" / f"{image_id}.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(json.dumps({
        "image_id": image_id, "run_id": f"run-{image_id}", "profile": profile,
        "region": "ap-guangzhou", "state": state, "approved_at": "2026-08-26T00:00:00Z",
    }), encoding="utf-8")
    register_release(release, tmp_path / "registry")


def test_promote_and_resolve_channel(tmp_path):
    _artifact(tmp_path)
    pointer = promote_channel("rhel10", "stable", "img-1", actor="ci", reason="passed",
                              root=tmp_path / "registry")
    assert pointer["generation"] == 1
    assert pointer["previous_artifact_id"] is None
    assert resolve_channel("rhel10", "stable", tmp_path / "registry")["artifact"]["artifact_id"] == "img-1"


def test_promote_uses_generation_compare_and_swap(tmp_path):
    _artifact(tmp_path, "img-1")
    _artifact(tmp_path, "img-2")
    promote_channel("rhel10", "stable", "img-1", expected_generation=0,
                    root=tmp_path / "registry")
    with pytest.raises(ChannelConflictError, match="generation is 1, expected 0"):
        promote_channel("rhel10", "stable", "img-2", expected_generation=0,
                        root=tmp_path / "registry")
    pointer = promote_channel("rhel10", "stable", "img-2", expected_generation=1,
                              root=tmp_path / "registry")
    assert pointer["generation"] == 2
    assert pointer["previous_artifact_id"] == "img-1"


def test_promote_rejects_wrong_bucket_and_revoked_artifact(tmp_path):
    _artifact(tmp_path, "img-1", "rhel10")
    _artifact(tmp_path, "img-2", "rhel10", "revoked")
    with pytest.raises(ValueError, match="does not belong"):
        promote_channel("ubuntu2404", "stable", "img-1", root=tmp_path / "registry")
    with pytest.raises(ValueError, match="not active"):
        promote_channel("rhel10", "stable", "img-2", root=tmp_path / "registry")


def test_resolve_rejects_tampered_pointer(tmp_path):
    _artifact(tmp_path)
    promote_channel("rhel10", "stable", "img-1", root=tmp_path / "registry")
    path = tmp_path / "registry" / "channels" / "rhel10" / "stable.json"
    doc = json.loads(path.read_text())
    doc["artifact_id"] = "img-attacker"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="integrity"):
        resolve_channel("rhel10", "stable", tmp_path / "registry")
    assert len(collect_channels(tmp_path / "registry", "rhel10")) == 1


def test_quarantine_automatically_rolls_channel_back(tmp_path):
    _artifact(tmp_path, "img-1")
    _artifact(tmp_path, "img-2")
    promote_channel("rhel10", "stable", "img-1", root=tmp_path / "registry")
    promote_channel("rhel10", "stable", "img-2", root=tmp_path / "registry")
    result = change_artifact_status(
        "img-2", "quarantined", actor="security", reason="critical CVE",
        root=tmp_path / "registry")
    assert result["artifact"]["status"] == "quarantined"
    assert result["channel_rollbacks"][0]["status"] == "rolled_back"
    resolved = resolve_channel("rhel10", "stable", tmp_path / "registry")
    assert resolved["artifact"]["artifact_id"] == "img-1"


def test_revoke_without_previous_blocks_channel_and_resolution(tmp_path):
    _artifact(tmp_path, "img-1")
    promote_channel("rhel10", "stable", "img-1", root=tmp_path / "registry")
    result = change_artifact_status(
        "img-1", "revoked", actor="security", reason="compromised",
        root=tmp_path / "registry")
    assert result["channel_rollbacks"][0]["status"] == "blocked"
    with pytest.raises(ValueError, match="not active"):
        resolve_channel("rhel10", "stable", tmp_path / "registry")


def test_revocation_is_permanent(tmp_path):
    _artifact(tmp_path, "img-1")
    change_artifact_status("img-1", "revoked", actor="security", reason="compromised",
                           root=tmp_path / "registry")
    with pytest.raises(ValueError, match="permanently revoked"):
        change_artifact_status("img-1", "quarantined", actor="ops", reason="retry",
                               root=tmp_path / "registry")
