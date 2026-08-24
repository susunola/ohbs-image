from __future__ import annotations

import argparse
import json

from ohbs_image._report_diff import cmd_report_diff


def test_report_diff_emits_changed_fields(tmp_path, monkeypatch, capsys):
    lineage = tmp_path / "lineage.jsonl"
    lineage.write_text(
        json.dumps({"run_id": "before", "score": 90, "source_image_id": "img-1"}) + "\n" +
        json.dumps({"run_id": "after", "score": 96, "source_image_id": "img-2"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr("ohbs_image._report_diff._lineage_path", lambda: lineage)
    args = argparse.Namespace(before="before", after="after", output="json")
    assert cmd_report_diff(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {c["field"] for c in doc["changes"]} == {"score", "source_image_id"}
