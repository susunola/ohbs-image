from __future__ import annotations

import json

from ohbs_image._ancestry import link_parent
from ohbs_image._rebuild_events import EVENT_SCHEMA, plan_rebuild_event, process_rebuild_event
from ohbs_image._registry import _artifact_path, _read_object, register_release


def _artifact(tmp_path, image_id):
    release = tmp_path / "releases" / f"{image_id}.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(json.dumps({"image_id": image_id, "run_id": f"run-{image_id}",
        "profile": "rhel10", "region": "ap-guangzhou", "state": "approved"}), encoding="utf-8")
    register_release(release, tmp_path / "registry")


def _event():
    return {"schema": EVENT_SCHEMA, "event_id": "evt-42", "type": "cve.detected",
            "artifact_id": "img-parent", "cve_id": "CVE-2026-0042",
            "occurred_at": "2026-08-26T12:00:00Z"}


def test_event_dry_run_has_blast_radius_without_mutation(tmp_path):
    _artifact(tmp_path, "img-parent")
    _artifact(tmp_path, "img-child")
    root = tmp_path / "registry"
    link_parent("img-child", "img-parent", root=root)
    plan = plan_rebuild_event(_event(), root)
    assert [item["artifact_id"] for item in plan["actions"]] == ["img-parent", "img-child"]
    assert _read_object(_artifact_path("img-parent", root))["status"] == "active"


def test_apply_quarantines_and_queues_descendant_first_idempotently(tmp_path):
    _artifact(tmp_path, "img-parent")
    _artifact(tmp_path, "img-child")
    root = tmp_path / "registry"
    link_parent("img-child", "img-parent", root=root)
    first = process_rebuild_event(_event(), apply=True, root=root)
    replay = process_rebuild_event(_event(), apply=True, root=root)
    assert replay == first
    assert [item["artifact_id"] for item in first["results"]] == ["img-child", "img-parent"]
    assert first["queued"] == 2
    assert len(list((root / "rebuild_requests").glob("*.json"))) == 2
    assert _read_object(_artifact_path("img-child", root))["status"] == "quarantined"


def test_event_validation_requires_cve_id(tmp_path):
    event = _event()
    event.pop("cve_id")
    try:
        plan_rebuild_event(event, tmp_path)
    except ValueError as exc:
        assert "cve_id" in str(exc)
    else:
        raise AssertionError("invalid event unexpectedly accepted")
