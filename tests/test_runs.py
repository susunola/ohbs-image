from __future__ import annotations

import argparse
import json

from ohbs_image._runs import cmd_run_list, cmd_run_show, collect_runs


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def test_collect_runs_joins_artifacts_and_keeps_plan_only_run(tmp_path):
    (tmp_path / "lineage.jsonl").write_text(json.dumps({
        "run_id": "built-1", "ts": "2026-08-26T02:00:00Z", "status": "ok",
        "mode": "build", "profile": "tencentos3"}) + "\n", encoding="utf-8")
    _write(tmp_path / "runs" / "built-1.json", {
        "run_id": "built-1", "status": "ok", "phase": "complete"})
    _write(tmp_path / "plans" / "planned-1-plan.json", {
        "run_id": "planned-1", "generated_at": "2026-08-26T01:00:00Z",
        "profile": "ubuntu2404"})
    _write(tmp_path / "releases" / "img-1.json", {
        "run_id": "built-1", "image_id": "img-1"})
    _write(tmp_path / "acceptance" / "built-1.json", {
        "runId": "built-1", "status": "passed"})

    rows = collect_runs(tmp_path)

    assert [row["run_id"] for row in rows] == ["built-1", "planned-1"]
    assert rows[0]["evidence_count"] == 4
    assert rows[1]["status"] == "planned"


def test_run_list_json_filters(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "plans" / "one-plan.json", {
        "run_id": "one", "profile": "ubuntu2404", "generated_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr("ohbs_image._runs._lineage_path", lambda: tmp_path / "lineage.jsonl")

    assert cmd_run_list(argparse.Namespace(
        limit=20, profile="ubuntu2404", status="planned", output="json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["count"] == 1
    assert doc["runs"][0]["run_id"] == "one"


def test_run_show_lists_all_evidence(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "runs" / "run-1.json", {"run_id": "run-1", "status": "failed"})
    monkeypatch.setattr("ohbs_image._runs._lineage_path", lambda: tmp_path / "lineage.jsonl")

    assert cmd_run_show(argparse.Namespace(run_id="run-1", output="text")) == 0
    output = capsys.readouterr().out
    assert "status: failed" in output
    assert "runs/run-1.json" in output


def test_run_show_unknown_returns_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("ohbs_image._runs._lineage_path", lambda: tmp_path / "lineage.jsonl")
    assert cmd_run_show(argparse.Namespace(run_id="missing", output="json")) == 1
    assert "No state artifacts" in caplog.text
