from __future__ import annotations

import json

from ohbs_image._registry import _database, collect_artifacts, get_artifact, register_release


def _release(path, image_id="img-db", run_id="run-1", profile="rhel10"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"image_id": image_id, "run_id": run_id,
        "profile": profile, "region": "ap-guangzhou", "state": "approved",
        "approved_at": "2026-08-26T00:00:00Z"}), encoding="utf-8")


def test_database_is_primary_when_json_mirror_is_missing(tmp_path):
    root = tmp_path / "registry"
    release = tmp_path / "releases" / "img-db.json"
    _release(release)
    mirror = register_release(release, root)
    mirror.unlink()

    assert get_artifact("img-db", root)["version"] == "run-1"
    assert [row["artifact_id"] for row in collect_artifacts(root)] == ["img-db"]


def test_database_searches_indexed_fields_and_labels(tmp_path):
    root = tmp_path / "registry"
    release = tmp_path / "releases" / "img-search.json"
    _release(release, image_id="img-search", run_id="v42", profile="rhel10")
    register_release(release, root)

    count, rows = _database(root).search_artifacts(
        bucket="rhel10", version="v42", query="search", label="profile=rhel10")
    assert count == 1
    assert rows[0]["artifact_id"] == "img-search"
