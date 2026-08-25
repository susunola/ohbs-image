from __future__ import annotations

import argparse
import json

from ohbs_image._report_diff import cmd_report_diff, cmd_report_list, cmd_report_show


def _lineage(tmp_path, rows):
    lineage = tmp_path / "lineage.jsonl"
    lineage.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return lineage


def test_report_diff_emits_changed_fields(tmp_path, monkeypatch, capsys):
    lineage = _lineage(tmp_path, [
        {"run_id": "before", "score": 90, "source_image_id": "img-1"},
        {"run_id": "after", "score": 96, "source_image_id": "img-2"},
    ])
    monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
    args = argparse.Namespace(before="before", after="after", output="json")
    assert cmd_report_diff(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {c["field"] for c in doc["changes"]} == {"score", "source_image_id"}


# ------------------------------------------------------- roadmap F
class TestReportList:
    def _rows(self):
        return [
            {"ts": "2026-08-25T10:00:00Z", "run_id": "run-1", "status": "ok",
             "mode": "build", "profile": "tencentos3", "cis_level": 1,
             "score": 95.0, "image_name": "img-a", "region": "ap-guangzhou"},
            {"ts": "2026-08-25T11:00:00Z", "run_id": "run-2", "status": "failed",
             "mode": "build", "profile": "ubuntu2404", "cis_level": 2,
             "score": None, "image_name": "", "region": "ap-singapore"},
            {"ts": "2026-08-25T12:00:00Z", "run_id": "run-3", "status": "ok",
             "mode": "scan", "profile": "tencentos3", "cis_level": 1,
             "score": 88.0, "image_name": "", "region": "ap-guangzhou"},
        ]

    def test_json_contract_and_order(self, tmp_path, monkeypatch, capsys):
        lineage = _lineage(tmp_path, self._rows())
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_list(argparse.Namespace(
            limit=20, profile=None, status=None, mode=None, output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["schema"].endswith("/report-list/v1")
        assert doc["count"] == 3
        assert doc["records"][0]["run_id"] == "run-3"  # newest first

    def test_filters(self, tmp_path, monkeypatch, capsys):
        lineage = _lineage(tmp_path, self._rows())
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_list(argparse.Namespace(
            limit=20, profile="tencentos3", status=None, mode=None,
            output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["count"] == 2
        assert {r["run_id"] for r in doc["records"]} == {"run-3", "run-1"}

    def test_status_and_mode_filters(self, tmp_path, monkeypatch, capsys):
        lineage = _lineage(tmp_path, self._rows())
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_list(argparse.Namespace(
            limit=20, profile=None, status="ok", mode="scan",
            output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["count"] == 1 and doc["records"][0]["run_id"] == "run-3"

    def test_limit(self, tmp_path, monkeypatch, capsys):
        lineage = _lineage(tmp_path, self._rows())
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_list(argparse.Namespace(
            limit=2, profile=None, status=None, mode=None, output="json")) == 0
        assert len(json.loads(capsys.readouterr().out)["records"]) == 2

    def test_empty_lineage(self, tmp_path, monkeypatch, capsys):
        lineage = _lineage(tmp_path, [])
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_list(argparse.Namespace(
            limit=20, profile=None, status=None, mode=None, output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["count"] == 0

    def test_text_output(self, tmp_path, monkeypatch, capsys):
        lineage = _lineage(tmp_path, self._rows()[:1])
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_list(argparse.Namespace(
            limit=20, profile=None, status=None, mode=None, output="text")) == 0
        out = capsys.readouterr().out
        assert "run-1" in out and "tencentos3" in out


class TestReportShow:
    def test_show_existing_run(self, tmp_path, monkeypatch, capsys):
        row = {"ts": "2026-08-25T10:00:00Z", "run_id": "run-1", "status": "ok",
               "mode": "build", "profile": "tencentos3", "cis_level": 1,
               "score": 95.0, "image_name": "img-a", "region": "ap-guangzhou",
               "source_image_id": "img-src", "fingerprint": "fp"}
        lineage = _lineage(tmp_path, [row])
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        monkeypatch.setattr(
            "ohbs_image._report_diff._read_run_manifest", lambda run_id: None)
        assert cmd_report_show(argparse.Namespace(run_id="run-1", output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["record"]["status"] == "ok"
        assert doc["schema"].endswith("/report-show/v1")

    def test_show_unknown_run(self, tmp_path, monkeypatch, caplog):
        lineage = _lineage(tmp_path, [{"run_id": "run-1"}])
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        assert cmd_report_show(argparse.Namespace(run_id="nope", output="text")) == 1
        assert "No lineage record" in caplog.text

    def test_show_attaches_manifest(self, tmp_path, monkeypatch, capsys):
        row = {"ts": "2026-08-25T10:00:00Z", "run_id": "run-1", "status": "ok",
               "mode": "build", "profile": "tencentos3", "cis_level": 1}
        lineage = _lineage(tmp_path, [row])
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
        monkeypatch.setattr(
            "ohbs_image._report_diff._read_run_manifest",
            lambda run_id: {"status": "ok", "phase": "packer-build"})
        assert cmd_report_show(argparse.Namespace(run_id="run-1", output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["manifest"]["phase"] == "packer-build"
