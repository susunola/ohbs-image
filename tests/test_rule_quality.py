from __future__ import annotations

import argparse
import json

from ohbs_image._rule_quality import (
    RULE_QUALITY_REPORT_SCHEMA,
    assess_catalog,
    assess_rule,
    build_quality_report,
    cmd_catalog_lint,
    render_quality_html,
)


def _rule(**overrides):
    value = {
        "id": "1.1.1", "title": "Ensure example", "section": "1.1",
        "levels": [1], "platforms": ["Server"], "assessment": "Automated",
        "family": "sysctl", "params": {"key": "example", "value": 1},
        "risk": "safe", "page": 10,
    }
    value.update(overrides)
    return value


def test_complete_rule_receives_a_grade_and_machine_dimensions() -> None:
    result = assess_rule(_rule(), {
        "description": "what", "rationale": "why", "remediation": "fix",
        "audit": "check", "impact": "none", "rollback": "restore",
    }, strict=True)
    assert result["grade"] == "A"
    assert result["score"] == 100.0
    assert all(result["dimensions"].values())
    assert result["issues"] == []


def test_strict_lint_promotes_automation_conflict_to_error() -> None:
    relaxed = assess_rule(_rule(family="manual", automated=False), {}, strict=False)
    strict = assess_rule(_rule(family="manual", automated=False), {}, strict=True)
    assert relaxed["issues"][0]["severity"] == "warning"
    assert strict["issues"][0]["severity"] == "error"


def test_catalog_baseline_counts_quality_dimensions(tmp_path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([_rule(), _rule(id="1.1.2", risk="disruptive")]), encoding="utf-8")
    (tmp_path / "guidance.json").write_text(json.dumps({
        "1.1.1": {"description": "what", "remediation": "fix"},
        "1.1.2": {"description": "what"},
    }), encoding="utf-8")
    result = assess_catalog("test", path)
    assert result["rules"] == 2
    assert result["dimensions"]["guidance"] == 2
    assert result["dimensions"]["remediation"] == 1
    assert result["dimensions"]["rollback"] == 0


def test_full_report_is_truthful_and_html_is_self_contained() -> None:
    report = build_quality_report(strict=False, profile="rhel10")
    assert report["schema"] == RULE_QUALITY_REPORT_SCHEMA
    assert report["summary"]["profiles"] == 1
    assert report["summary"]["rules"] > 250
    assert len(report["priority_rules"]) == 50
    page = render_quality_html(report)
    assert "<!doctype html>" in page
    assert "优先治理 50 条" in page
    assert "不代表 CIS 认证" in page


def test_command_writes_report_and_strict_mode_exposes_existing_conflicts(tmp_path, capsys) -> None:
    target = tmp_path / "quality.html"
    relaxed = argparse.Namespace(strict=False, profile="rhel10", output="json", report=str(target))
    assert cmd_catalog_lint(relaxed) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["rules"] > 250
    assert target.is_file()
    strict = argparse.Namespace(strict=True, profile="rhel10", output="json", report="")
    rc = cmd_catalog_lint(strict)
    strict_result = json.loads(capsys.readouterr().out)
    assert rc == (0 if strict_result["ok"] else 1)
