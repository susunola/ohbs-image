from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta

from ohbs_image._state import cmd_state_verify, verify_state


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def test_valid_state_and_release_hash(tmp_path):
    run_id = "11111111-1111-1111-1111-111111111111"
    (tmp_path / "lineage.jsonl").write_text(
        json.dumps({"run_id": run_id, "status": "ok"}) + "\n", encoding="utf-8")
    _write(tmp_path / "runs" / f"{run_id}.json", {
        "run_id": run_id, "status": "completed", "phase": "complete"})
    report = tmp_path / "reports" / f"image.{run_id}.json"
    report.parent.mkdir()
    report.write_text("evidence", encoding="utf-8")
    digest = hashlib.sha256(b"evidence").hexdigest()
    _write(tmp_path / "releases" / "img-1.json", {
        "run_id": run_id,
        "evidence": {"audit_report": str(report.relative_to(tmp_path)),
                     "audit_sha256": digest}})

    result = verify_state(tmp_path)

    assert result["valid"] is True
    assert result["errors"] == 0
    assert result["runs"] == 1


def test_detects_corruption_duplicate_and_expired_lease(tmp_path):
    run_id = "22222222-2222-2222-2222-222222222222"
    (tmp_path / "lineage.jsonl").write_text(
        json.dumps({"run_id": run_id}) + "\n" + json.dumps({"run_id": run_id}) + "\n",
        encoding="utf-8")
    _write(tmp_path / "runs" / f"{run_id}.json", {
        "run_id": run_id, "status": "active",
        "lease_expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()})
    broken = tmp_path / "plans" / f"{run_id}-plan.json"
    broken.parent.mkdir()
    broken.write_text("{broken", encoding="utf-8")

    result = verify_state(tmp_path)

    codes = {item["code"] for item in result["findings"]}
    assert {"duplicate-run-id", "expired-active-lease", "invalid-json"} <= codes
    assert result["valid"] is False


def test_detects_release_digest_mismatch_and_path_escape(tmp_path):
    outside = tmp_path.parent / "outside-evidence"
    outside.write_text("secret", encoding="utf-8")
    _write(tmp_path / "releases" / "img-1.json", {
        "run_id": "missing-run",
        "evidence": {
            "audit_report": "../outside-evidence", "audit_sha256": "0" * 64,
            "html_report": "reports/missing.html", "html_report_sha256": "1" * 64,
        }})

    result = verify_state(tmp_path)

    codes = {item["code"] for item in result["findings"]}
    assert "evidence-path-escape" in codes
    assert "missing-evidence" in codes
    assert "unknown-release-run" in codes


def test_strict_promotes_warning_to_nonzero(tmp_path, monkeypatch, capsys):
    run_id = "33333333-3333-3333-3333-333333333333"
    _write(tmp_path / "releases" / "img-1.json", {
        "run_id": run_id, "evidence": {}})
    monkeypatch.setattr("ohbs_image._state._state_dir", lambda: tmp_path)

    assert cmd_state_verify(argparse.Namespace(output="json", strict=False)) == 0
    capsys.readouterr()
    assert cmd_state_verify(argparse.Namespace(output="json", strict=True)) == 1
