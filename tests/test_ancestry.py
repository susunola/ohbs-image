import json

import pytest

from ohbs_image._ancestry import (
    cascade_revoke,
    descendants,
    impact_plan,
    link_parent,
    verify_ancestry,
)
from ohbs_image._channels import promote_channel
from ohbs_image._registry import _hash, register_release


def _artifact(tmp_path, image_id, source=None):
    release = tmp_path / "releases" / f"{image_id}.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(json.dumps({
        "image_id": image_id, "run_id": f"run-{image_id}", "profile": "rhel10",
        "region": "ap-guangzhou", "state": "approved",
        "source_image_id": source,
    }))
    register_release(release, tmp_path / "registry")


def test_release_source_is_registered_as_parent(tmp_path):
    _artifact(tmp_path, "img-child", "img-vendor")
    doc = json.loads((tmp_path / "registry/artifacts/img-child.json").read_text())
    assert doc["parents"] == [{"artifact_id": "img-vendor",
                               "relation": "derived_from", "external": True}]


def test_descendants_are_transitive_with_depth(tmp_path):
    for image_id in ("img-root", "img-child", "img-grandchild"):
        _artifact(tmp_path, image_id)
    root = tmp_path / "registry"
    link_parent("img-child", "img-root", root=root)
    link_parent("img-grandchild", "img-child", root=root)
    assert [(row["artifact_id"], row["depth"]) for row in descendants("img-root", root)] == [
        ("img-child", 1), ("img-grandchild", 2)]


def test_link_rejects_cycle_and_restores_graph(tmp_path):
    _artifact(tmp_path, "img-a")
    _artifact(tmp_path, "img-b")
    root = tmp_path / "registry"
    link_parent("img-b", "img-a", root=root)
    with pytest.raises(ValueError, match="ancestry cycle"):
        link_parent("img-a", "img-b", root=root)
    assert verify_ancestry(root) == []
    assert descendants("img-a", root)[0]["artifact_id"] == "img-b"


def test_reregister_preserves_governance_parent_edge(tmp_path):
    _artifact(tmp_path, "img-parent")
    _artifact(tmp_path, "img-child")
    root = tmp_path / "registry"
    link_parent("img-child", "img-parent", root=root)
    register_release(tmp_path / "releases/img-child.json", root)
    assert descendants("img-parent", root)[0]["artifact_id"] == "img-child"


def test_impact_includes_descendants_and_channels(tmp_path):
    _artifact(tmp_path, "img-root")
    _artifact(tmp_path, "img-child")
    root = tmp_path / "registry"
    link_parent("img-child", "img-root", root=root)
    promote_channel("rhel10", "stable", "img-child", root=root)
    plan = impact_plan("img-root", root)
    assert plan["affected_count"] == 2
    assert plan["descendant_count"] == 1
    assert plan["channels"][0]["channel"] == "stable"


def test_cascade_revoke_is_dry_run_by_default(tmp_path):
    _artifact(tmp_path, "img-root")
    _artifact(tmp_path, "img-child")
    root = tmp_path / "registry"
    link_parent("img-child", "img-root", root=root)
    plan = cascade_revoke("img-root", actor="security", reason="CVE", root=root)
    assert plan["apply"] is False and plan["results"] == []
    assert plan["document_hash"] == _hash(plan)
    child = json.loads((root / "artifacts/img-child.json").read_text())
    assert child["status"] == "active"


def test_cascade_revoke_applies_deepest_first(tmp_path):
    _artifact(tmp_path, "img-root")
    _artifact(tmp_path, "img-child")
    root = tmp_path / "registry"
    link_parent("img-child", "img-root", root=root)
    result = cascade_revoke("img-root", actor="security", reason="CVE",
                            apply=True, root=root)
    assert [row["artifact_id"] for row in result["results"]] == ["img-child", "img-root"]
    assert all(row["status"] == "revoked" for row in result["results"])
    assert result["document_hash"] == _hash(result)
