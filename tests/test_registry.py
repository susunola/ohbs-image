import argparse
import json

from ohbs_image._registry import (
    cmd_registry_list,
    collect_artifacts,
    rebuild_registry,
    register_release,
    verify_registry,
)


def _release(path, image_id="img-1", run_id="run-1", profile="rhel10"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "https://ohbs-image.dev/release-manifest/v1",
        "image_id": image_id, "image_name": "gold", "state": "approved",
        "approved_at": "2026-08-26T00:00:00Z", "run_id": run_id,
        "profile": profile, "cis_level": 1, "region": "ap-guangzhou",
        "score": 98.5, "attestation_signed": True, "evidence": {},
    }), encoding="utf-8")


def test_register_release_builds_versioned_artifact(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    _release(release)
    destination = register_release(release, tmp_path / "registry")
    doc = json.loads(destination.read_text())
    assert doc["artifact_id"] == "img-1"
    assert doc["bucket"] == "rhel10"
    assert doc["version"] == "run-1"
    assert doc["attestation_signed"] is True
    assert verify_registry(tmp_path / "registry") == []


def test_rebuild_registry_indexes_all_releases(tmp_path):
    _release(tmp_path / "releases" / "img-1.json")
    _release(tmp_path / "releases" / "img-2.json", "img-2", "run-2", "ubuntu2404")
    result = rebuild_registry(tmp_path, tmp_path / "registry")
    assert result["registered"] == 2
    assert result["index"]["artifact_count"] == 2
    assert result["index"]["buckets"] == ["rhel10", "ubuntu2404"]
    assert len(collect_artifacts(tmp_path / "registry")) == 2


def test_registry_verify_detects_tampering(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    _release(release)
    path = register_release(release, tmp_path / "registry")
    doc = json.loads(path.read_text())
    doc["score"] = 1
    path.write_text(json.dumps(doc))
    assert verify_registry(tmp_path / "registry") == ["img-1: document hash mismatch"]


def test_registry_list_json(tmp_path, monkeypatch, capsys):
    release = tmp_path / "releases" / "img-1.json"
    _release(release)
    register_release(release, tmp_path / "registry")
    monkeypatch.setattr("ohbs_image._registry._lineage_path",
                        lambda: tmp_path / "lineage.jsonl")
    assert cmd_registry_list(argparse.Namespace(
        bucket="rhel10", status="active", output="json")) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
