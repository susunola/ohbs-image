from __future__ import annotations

import json

import pytest

from ohbs_image._audit_compare import compare_audits, load_audit
from ohbs_image._logging import ConfigError


def test_json_to_xccdf_comparison_classifies_conflicts_and_denominator(tmp_path):
    internal = tmp_path / "internal.json"
    internal.write_text(json.dumps({"benchmark": "CIS-v1", "results": [
        {"id": "1.1.1", "status": "pass"},
        {"id": "1.1.2", "status": "fail"},
        {"id": "1.1.3", "status": "manual"},
    ]}), encoding="utf-8")
    external = tmp_path / "external.xml"
    external.write_text("""<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2">
      <TestResult benchmark-reference="CIS-v1">
       <rule-result idref="xccdf_org.ohbs_image.content_rule_1.1.1"><result>pass</result></rule-result>
       <rule-result idref="xccdf_org.ohbs_image.content_rule_1.1.2"><result>pass</result></rule-result>
       <rule-result idref="external_only"><result>fail</result></rule-result>
      </TestResult></Benchmark>""", encoding="utf-8")
    result = compare_audits(load_audit(internal), load_audit(external))
    assert result["summary"] == {
        "internal_rules": 3, "external_rules": 3, "shared_rules": 2,
        "agreements": 1, "conflicts": 1, "unknown_status": 0,
        "overlap_percent": 50.0, "min_overlap_percent": 0.0}
    assert result["conflicts"][0]["rule_id"] == "1.1.2"
    assert result["only_internal"] == ["1.1.3"]
    assert result["only_external"] == ["external_only"]
    assert result["passed"] is False


def test_benchmark_mismatch_is_not_comparable_even_when_rules_agree(tmp_path):
    one = tmp_path / "one.json"
    two = tmp_path / "two.json"
    one.write_text(json.dumps({"benchmark": "CIS-v1", "results": [
        {"id": "1", "status": "pass"}]}), encoding="utf-8")
    two.write_text(json.dumps({"benchmark": "CIS-v2", "results": [
        {"id": "1", "status": "pass"}]}), encoding="utf-8")
    result = compare_audits(load_audit(one), load_audit(two))
    assert result["benchmark_mismatch"] is True
    assert result["passed"] is False


def test_overlap_gate_exposes_unmapped_rule_ids(tmp_path):
    left = {"results": {"1": "pass", "2": "pass"}, "benchmark": ""}
    right = {"results": {"1": "pass", "other": "pass"}, "benchmark": ""}
    result = compare_audits(left, right, min_overlap_percent=50)
    assert result["summary"]["overlap_percent"] == pytest.approx(33.333)
    assert result["passed"] is False


def test_duplicate_rule_ids_fail_closed(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps({"results": [
        {"id": "1", "status": "pass"}, {"id": "1", "status": "fail"}]}))
    with pytest.raises(ConfigError, match="duplicate rule ID"):
        load_audit(path)


def test_overlap_gate_range_is_validated():
    with pytest.raises(ConfigError, match="between 0 and 100"):
        compare_audits({"results": {}}, {"results": {}}, min_overlap_percent=101)


@pytest.mark.parametrize("results", [{}, {"1": "unknown"}, {"1": "error"},
                                   {"1": "not_evaluated"}])
def test_inconclusive_evidence_cannot_pass_comparison(results):
    audit = {"benchmark": "CIS-v1", "results": results}
    assert compare_audits(audit, audit)["passed"] is False


def test_apply_success_is_not_scan_success(tmp_path):
    path = tmp_path / "apply.json"
    path.write_text(json.dumps({"benchmark": "CIS-v1", "results": [
        {"id": "1", "status": "applied"}]}))
    audit = load_audit(path)
    assert audit["results"]["1"] == "not_evaluated"
    assert compare_audits(audit, audit)["passed"] is False
