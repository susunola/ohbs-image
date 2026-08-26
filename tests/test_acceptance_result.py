from __future__ import annotations

import argparse
import json

from scripts.write_acceptance_result import build_result, main


def test_build_result_maps_action_outcome_to_portable_status(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_REPOSITORY", "susunola/ohbs-image")
    args = argparse.Namespace(
        status="success", profile="ubuntu2404", level="1", artifact="acceptance-42",
        commit="abc123", workflow="cloud-canary", run_url="", started_at="2026-08-26T01:00:00Z",
        build_instance_type="SA5.MEDIUM2")

    result = build_result(args)

    assert result["status"] == "passed"
    assert result["level"] == 1
    assert result["runAttempt"] == 2
    assert result["runUrl"] == "https://github.com/susunola/ohbs-image/actions/runs/42"


def test_main_writes_failed_result_and_markdown_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    monkeypatch.setenv("GITHUB_WORKFLOW", "real-cloud-acceptance")
    output = tmp_path / "result.json"
    summary = tmp_path / "summary.md"

    assert main([
        "--status", "failure", "--profile", "win2022", "--level", "2",
        "--artifact", "real-cloud-acceptance-win2022-9",
        "--output", str(output), "--summary", str(summary),
    ]) == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["profile"] == "win2022"
    assert "FAILED" in summary.read_text(encoding="utf-8")
    assert "real-cloud-acceptance-win2022-9" in summary.read_text(encoding="utf-8")
