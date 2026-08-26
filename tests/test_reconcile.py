import argparse
import json
from datetime import UTC, datetime, timedelta

from ohbs_image._reconcile import cmd_state_reconcile, plan_reconciliation
from ohbs_image._run_events import append_run_event, read_run_events


def _manifest(root, run_id, **values):
    path = root / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"run_id": run_id, "status": "active", "state": "BUILDING", "resources": []}
    doc.update(values)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_reconcile_plan_finds_expired_and_orphan_records(tmp_path):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _manifest(tmp_path, "expired", lease_expires_at=past)
    _manifest(tmp_path, "orphan", status="failed", state="FAILED",
              resources=[{"type": "instance", "id": "ins-1"}])
    plan = plan_reconciliation(tmp_path)
    assert [item["action"] for item in plan["actions"]] == [
        "expire_run", "inspect_orphan_resources"]
    assert plan["safe_count"] == 1


def test_reconcile_apply_marks_expired_run_failed(tmp_path, monkeypatch, capsys):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    path = _manifest(tmp_path, "run-1", lease_expires_at=past)
    append_run_event("run-1", "CREATED", root=tmp_path)
    append_run_event("run-1", "BUILDING", root=tmp_path)
    monkeypatch.setattr("ohbs_image._reconcile._lineage_path",
                        lambda: tmp_path / "lineage.jsonl")
    assert cmd_state_reconcile(argparse.Namespace(apply=True, output="json")) == 0
    assert json.loads(path.read_text())["state"] == "FAILED"
    assert read_run_events("run-1", tmp_path)[-1]["phase"] == "lease-expired"
    assert json.loads(capsys.readouterr().out)["applied"] == 1


def test_reconcile_check_is_read_only(tmp_path, monkeypatch):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    path = _manifest(tmp_path, "run-2", lease_expires_at=past)
    before = path.read_bytes()
    monkeypatch.setattr("ohbs_image._reconcile._lineage_path",
                        lambda: tmp_path / "lineage.jsonl")
    assert cmd_state_reconcile(argparse.Namespace(apply=False, output="json")) == 0
    assert path.read_bytes() == before
