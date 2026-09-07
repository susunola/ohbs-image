from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ohbs_image._accuracy_baseline import (
    BASELINE_SCHEMA,
    MATRIX_SCHEMA,
    coverage_counts,
    create_baseline,
    explain_gaps,
    rule_ledger,
    summarize_repeats,
    validate_matrix,
    verify_baseline,
)
from ohbs_image._cli import build_parser


def _matrix() -> dict:
    return {"schema": MATRIX_SCHEMA, "targets": [{"profile": "rocky9", "level": 1,
        "source_image_id": "img-p6hcvn79", "region": "ap-guangzhou",
        "zone": "ap-guangzhou-6", "instance_type": "S5.MEDIUM2",
        "benchmark": "CIS-v2.0.0"}]}


def test_matrix_rejects_missing_or_duplicate_identity() -> None:
    document = _matrix()
    document["targets"].append(copy.deepcopy(document["targets"][0]))
    document["targets"][0]["source_image_id"] = ""
    failures = validate_matrix(document)
    assert any("missing source_image_id" in value for value in failures)
    assert any("duplicate profile/level" in value for value in failures)


def test_checked_in_tencent_linux_matrix_is_complete_and_valid() -> None:
    path = Path(__file__).with_name("golden-matrix-tencent-linux.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert validate_matrix(document) == []
    assert len(document["targets"]) == 12
    assert {(row["profile"], row["level"]) for row in document["targets"]} == {
        (profile, level) for profile in
        ("tencentos3", "tencentos4", "rocky9", "rhel8", "rhel9", "rhel10")
        for level in (1, 2)}


def test_coverage_formula_does_not_turn_skips_into_success() -> None:
    result = coverage_counts([{"status": value} for value in
                              ("pass", "pass", "fail", "manual", "notapplicable",
                               "notselected", "error")])
    assert result["applicable"] == 4
    assert result["coverage_percent"] == 50.0
    assert result["manual"] == result["not_applicable"] == result["not_scored"] == 1


def test_gap_explanation_accounts_for_every_non_pass_and_missing_rule() -> None:
    result = explain_gaps([
        {"id": "1", "status": "pass"},
        {"id": "2", "status": "fail", "apply_status": "applied_pending"},
        {"id": "3", "status": "error", "detail": "parser failed"},
        {"id": "4", "status": "manual"},
        {"id": "5", "status": "notapplicable"},
        {"id": "6", "status": "fail", "apply_status": "unsupported"},
        {"id": "7", "status": "fail"}],
        [{"id": str(index), "title": f"Rule {index}"} for index in range(1, 9)])
    assert result["category_counts"] == {"true_failure": 1, "detector_error": 1,
        "pending_reboot": 1, "environment_limited": 1, "manual": 1,
        "not_applicable": 1, "not_scored": 0, "implementation_missing": 1}
    assert result["coverage"]["applicable"] == 5
    assert result["coverage"]["coverage_percent"] == 20.0
    assert result["deduction_percent"] == 80.0


def test_missing_and_duplicate_evidence_cannot_claim_complete_coverage() -> None:
    report = explain_gaps([{"id": "1", "status": "pass"}] * 2,
                          [{"id": "1"}, {"id": "2"}])
    assert report["coverage"]["audit_pass_percent"] == 100
    assert report["evidence"]["complete"] is False
    assert report["evidence"]["result_coverage_percent"] == 50
    assert report["evidence"]["missing_rules"] == ["2"]
    assert report["evidence"]["duplicate_rules"] == ["1"]
    assert explain_gaps([])["evidence"]["complete"] is False
    counts = coverage_counts([{"status": "SKIP"}, {"status": "NA"}])
    assert counts["skip"] == counts["not_applicable"] == 1
    assert counts["audit_pass_percent"] is None


def test_rule_ledger_preserves_benchmark_page_and_automation(tmp_path: Path) -> None:
    role = tmp_path / "cis-test"
    (role / "files").mkdir(parents=True)
    (role / "files" / "rules.json").write_text(json.dumps([{
        "id": "1.1", "title": "Example", "section": "1", "page": 42,
        "assessment": "Manual", "levels": [1]}]), encoding="utf-8")
    result = rule_ledger(role, benchmark="CIS-v1")
    assert result["rules"][0] == {"id": "1.1", "title": "Example",
        "benchmark": "CIS-v1", "section": "1", "page": 42,
        "assessment": "Manual", "family": "", "levels": [1],
        "automation": "manual", "implementation": "bundled"}


def test_repeat_summary_surfaces_rule_variance_and_phase_p95() -> None:
    runs = [{"coverage": {"coverage_percent": 99},
             "results": [{"id": "1", "status": "pass"}],
             "phase_duration_seconds": {"launch": 10}},
            {"coverage": {"coverage_percent": 97},
             "results": [{"id": "1", "status": "fail"}],
             "phase_duration_seconds": {"launch": 20}},
            {"coverage": {"coverage_percent": 98},
             "results": [{"id": "1", "status": "pass"}],
             "phase_duration_seconds": {"launch": 15}}]
    result = summarize_repeats(runs)
    assert result["score"]["mean"] == 98
    assert result["unstable_rules"] == ["1"]
    assert result["phase_duration"]["launch"]["p95_seconds"] == 20
    with pytest.raises(ValueError):
        summarize_repeats(runs[:1])


def test_baseline_detects_nested_evidence_tampering() -> None:
    document = create_baseline(_matrix(), [{"run_id": "a", "raw": {"exit_code": 0}}],
                               [{"benchmark": "CIS-v2", "rules": []}])
    assert document["schema"] == BASELINE_SCHEMA
    assert verify_baseline(document)["valid"] is True
    document["inputs"]["runs"][0]["raw"]["exit_code"] = 1
    result = verify_baseline(document)
    assert result["valid"] is False
    assert "document_sha256 mismatch" in result["failures"]
    assert "run_sha256 mismatch" in result["failures"]


def test_baseline_cli_exposes_full_local_workflow() -> None:
    parser = build_parser()
    for argv, command in [
        (["baseline", "ledger", "--profile", "rocky9", "--output", "ledger.json"], "ledger"),
        (["baseline", "summarize", "--run", "a.json", "--run", "b.json"], "summarize"),
        (["baseline", "explain", "audit.json"], "explain"),
        (["baseline", "verify", "baseline.json"], "verify"),
    ]:
        args = parser.parse_args(argv)
        assert args.baseline_command == command
