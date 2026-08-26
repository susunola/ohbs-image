import argparse
import json
from datetime import UTC, datetime, timedelta

from ohbs_image._run_events import append_run_event
from ohbs_image._slo import calculate_run_slo, cmd_report_slo


def test_calculate_run_slo_counts_success_failure_and_retry(tmp_path):
    append_run_event("ok", "CREATED", root=tmp_path)
    append_run_event("ok", "BUILDING", root=tmp_path)
    append_run_event("ok", "APPROVED", root=tmp_path)
    append_run_event("bad", "CREATED", root=tmp_path)
    append_run_event("bad", "BUILDING", root=tmp_path)
    append_run_event("bad", "FAILED", metadata={"failure_category": "capacity"}, root=tmp_path)
    append_run_event("bad", "RETRYING", root=tmp_path)
    append_run_event("bad", "BUILDING", root=tmp_path)
    append_run_event("bad", "FAILED", metadata={"failure_category": "network"}, root=tmp_path)

    doc = calculate_run_slo(tmp_path)
    assert doc["runs"] == 2
    assert doc["successful"] == 1
    assert doc["failed"] == 1
    assert doc["retried"] == 1
    assert doc["success_rate"] == 50.0
    assert doc["failure_categories"] == {"network": 1}


def test_calculate_run_slo_excludes_old_runs(tmp_path):
    append_run_event("old", "CREATED", root=tmp_path)
    path = tmp_path / "events" / "old.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["timestamp"] = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert calculate_run_slo(tmp_path, days=30)["runs"] == 0


def test_cmd_report_slo_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ohbs_image._slo._lineage_path", lambda: tmp_path / "lineage.jsonl")
    assert cmd_report_slo(argparse.Namespace(days=7, output="json")) == 0
    assert json.loads(capsys.readouterr().out)["window_days"] == 7
