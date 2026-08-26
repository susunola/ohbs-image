from ohbs_image._evidence_center import EVIDENCE_SUMMARY_SCHEMA, summarize_evidence
from ohbs_image._registry import _hash


def test_evidence_summary_is_explicit_about_missing_proof() -> None:
    artifact = {"artifact_id": "img-1", "bucket": "linux", "version": "1",
                "status": "active", "score": 95, "critical_cves": 0}
    artifact["document_hash"] = _hash(artifact)
    result = summarize_evidence(artifact)
    assert result["schema"] == EVIDENCE_SUMMARY_SCHEMA
    assert result["passed"] >= 3
    failed = {check["name"] for check in result["checks"] if not check["passed"]}
    assert {"attestation", "provenance", "sbom", "clean_boot"} <= failed
