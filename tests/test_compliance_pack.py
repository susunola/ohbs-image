from __future__ import annotations

from ohbs_image._compliance_pack import assess_compliance, load_mapping, render_assessment_html


def test_mlps_pack_never_claims_certification() -> None:
    artifact = {"artifact_id": "img", "status": "active", "profile": "tencentos3",
                "score": 95, "critical_cves": 0,
                "evidence": {"offline_payload": True}}
    result = assess_compliance(load_mapping("mlps-2.0"), artifact)
    assert result["certification"] is False
    assert result["totals"]["gap"] > 0
    assert "not a grading" in result["disclaimer"]


def test_xinchuang_manual_controls_remain_manual() -> None:
    result = assess_compliance(load_mapping("xinchuang-readiness"),
                               {"artifact_id": "img", "profile": "tencentos4"})
    assert result["totals"]["manual"] == 3
    rendered = render_assessment_html(result)
    assert "MANUAL" in rendered and "certification" in rendered
