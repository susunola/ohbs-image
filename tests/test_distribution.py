import json

import pytest

from ohbs_image._distribution import distribution_plan, record_replica
from ohbs_image._registry import change_artifact_status, register_release, verify_registry


def _artifact(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({"image_id": "img-1", "run_id": "run-1",
                                   "profile": "rhel10", "region": "ap-guangzhou",
                                   "state": "approved"}))
    register_release(release, tmp_path / "registry")


def test_distribution_plan_skips_source_and_plans_missing_replica(tmp_path):
    _artifact(tmp_path)
    plan = distribution_plan("img-1", ["ap-guangzhou", "ap-shanghai"],
                             tmp_path / "registry")
    assert [(row["region"], row["action"], row["reason"]) for row in plan["actions"]] == [
        ("ap-guangzhou", "skip", "source-region"),
        ("ap-shanghai", "copy", "replica-missing"),
    ]
    assert plan["copy_count"] == 1


def test_recorded_replica_becomes_cache_hit_and_is_idempotent(tmp_path):
    _artifact(tmp_path)
    root = tmp_path / "registry"
    first = record_replica("img-1", "ap-shanghai", "img-copy-1", root=root)
    second = record_replica("img-1", "ap-shanghai", "img-copy-1", root=root)
    assert first == second
    plan = distribution_plan("img-1", ["ap-shanghai"], root)
    assert plan["cache_hits"] == 1
    assert plan["actions"][0]["replica_id"] == "img-copy-1"
    assert verify_registry(root) == []


def test_record_rejects_conflicting_replica(tmp_path):
    _artifact(tmp_path)
    root = tmp_path / "registry"
    record_replica("img-1", "ap-shanghai", "img-copy-1", root=root)
    with pytest.raises(ValueError, match="already has replica"):
        record_replica("img-1", "ap-shanghai", "img-copy-2", root=root)


def test_plan_deduplicates_regions_and_rejects_invalid_region(tmp_path):
    _artifact(tmp_path)
    root = tmp_path / "registry"
    assert len(distribution_plan("img-1", ["ap-shanghai", "ap-shanghai"], root)["actions"]) == 1
    with pytest.raises(ValueError, match="invalid region"):
        distribution_plan("img-1", ["not a region"], root)


def test_distribution_rejects_revoked_artifact(tmp_path):
    _artifact(tmp_path)
    root = tmp_path / "registry"
    change_artifact_status("img-1", "revoked", actor="security", reason="CVE", root=root)
    with pytest.raises(ValueError, match="not active"):
        distribution_plan("img-1", ["ap-shanghai"], root)
