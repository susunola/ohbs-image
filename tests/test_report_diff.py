from __future__ import annotations

import argparse
import json

import ohbs_image
from ohbs_image._report_diff import (cmd_report_diff, cmd_report_html,
                                     cmd_report_list, cmd_report_show)


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


# ------------------------------------------------------- roadmap F — html
def _html_row():
    return {"ts": "2026-08-25T10:00:00Z", "run_id": "run-html-1",
            "status": "ok", "mode": "build", "profile": "tencentos3",
            "cis_level": 1, "score": 96.5, "image_name": "release-tos3",
            "image_ids": ["img-abc"], "region": "ap-guangzhou",
            "zone": "ap-guangzhou-3", "source_image_id": "img-src",
            "benchmark": "CIS-v1.0.0"}


def _audit_json(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    doc = {"mode": "scan",
           "summary": {"all": {"pass": 120, "fail": 2, "manual": 1, "error": 0}},
           "results": [
               {"id": "1.1.1", "status": "pass", "apply_status": "applied",
                "title": "Ensure filesystem mounts"},
               {"id": "1.2.3", "status": "fail", "apply_status": "apply_failed",
                "title": "Ensure package updates"},
           ]}
    path = reports / "release-tos3.run-html-1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return reports


class TestReportHtml:
    def _wire(self, tmp_path, monkeypatch):
        lineage = _lineage(tmp_path, [_html_row()])
        monkeypatch.setattr("ohbs_image._report_diff._lineage_path",
                            lambda: lineage)
        monkeypatch.setattr(ohbs_image, "_reports_dir",
                            lambda: tmp_path / "reports")

    def test_renders_self_contained_page(self, tmp_path, monkeypatch, capsys):
        _audit_json(tmp_path)
        self._wire(tmp_path, monkeypatch)
        assert cmd_report_html(argparse.Namespace(run_id="run-html-1",
                                                  output=None)) == 0
        out = capsys.readouterr().out
        assert "run-html-1" in out
        page = (tmp_path / "reports" / "release-tos3.run-html-1.html").read_text(
            encoding="utf-8")
        assert "<!doctype html" in page
        assert "run-html-1" in page
        assert "96.5" in page
        assert "Ensure package updates" in page  # per-rule result rows
        assert "CIS-v1.0.0" in page

    def test_writes_to_explicit_dest(self, tmp_path, monkeypatch, capsys):
        _audit_json(tmp_path)
        self._wire(tmp_path, monkeypatch)
        dest = tmp_path / "customer" / "report.html"
        assert cmd_report_html(argparse.Namespace(
            run_id="run-html-1", output=str(dest))) == 0
        assert dest.is_file()
        assert "Assessment Results" in dest.read_text(encoding="utf-8")

    def test_missing_audit_still_renders_structure(self, tmp_path, monkeypatch,
                                                   capsys):
        # No audit JSON archived: the page still renders (structure only).
        (tmp_path / "reports").mkdir()
        self._wire(tmp_path, monkeypatch)
        assert cmd_report_html(argparse.Namespace(run_id="run-html-1",
                                                  output=None)) == 0
        page = (tmp_path / "reports" / "release-tos3.run-html-1.html").read_text(
            encoding="utf-8")
        assert "Not available" in page
        assert "run-html-1" in page

    def test_unknown_run_fails(self, tmp_path, monkeypatch, caplog):
        _audit_json(tmp_path)
        self._wire(tmp_path, monkeypatch)
        assert cmd_report_html(argparse.Namespace(run_id="nope",
                                                  output=None)) == 1
        assert "No lineage record" in caplog.text

    def test_scan_html_flag_registered(self):
        # `scan --html` must exist in the CLI surface (README parity gate).
        parser = ohbs_image.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        scan_parser = choices["scan"]
        html_act = next(a for a in scan_parser._actions if a.dest == "html")
        assert html_act.default is None
        report_parser = choices["report"]
        report_cmds = report_parser._subparsers._group_actions[0].choices
        assert "html" in report_cmds
        html_sub = report_cmds["html"]
        assert {a.dest for a in html_sub._actions} >= {"run_id", "output"}
