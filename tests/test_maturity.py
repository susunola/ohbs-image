from __future__ import annotations

from ohbs_image._accuracy_baseline import MATRIX_SCHEMA, create_baseline
from ohbs_image._maturity import MATURITY_SCHEMA, evaluate_maturity


def baseline():
    matrix = {"schema": MATRIX_SCHEMA, "targets": [{"profile": "tencentos4", "level": 1,
        "source_image_id": "img-source", "region": "ap-guangzhou",
        "zone": "ap-guangzhou-6", "instance_type": "S5.MEDIUM2",
        "benchmark": "CIS-v1.0.0"}]}
    return create_baseline(matrix, [], [])


def evidence(**release_overrides):
    release = {"clean_boot_verified": True, "signed": True, "rollback_verified": True,
               "compatibility_documented": True, "clean_boot_evidence": "clean.json",
               "provenance": "provenance.json", "channel_evidence": "channel.json",
               "compatibility_evidence": "compatibility.json"}
    release.update(release_overrides)
    return evaluate_maturity(
        baseline=baseline(), phase={"passed": True, "schema": "phase/v1"},
        release=release, sweep={"failed": 0, "resources": 0, "evidence_path": "sweep.json"},
        fault_matrix={"passed": True, "evidence_path": "fault.json"})


def test_maturity_requires_every_evidence_gate() -> None:
    result = evidence()
    assert result["schema"] == MATURITY_SCHEMA
    assert result["production_ready"] is True
    assert result["summary"] == {"passed": 8, "failed": 0, "total": 8}


def test_maturity_never_hides_missing_clean_boot_or_rollback() -> None:
    result = evidence(clean_boot_verified=False, rollback_verified=False)
    assert result["production_ready"] is False
    assert result["failed_checks"] == ["clean-boot", "rollback"]
