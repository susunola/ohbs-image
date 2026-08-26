from __future__ import annotations

from typing import Any

from ._registry import _hash

EVIDENCE_SUMMARY_SCHEMA = "https://ohbs-image.dev/evidence-summary/v1"


def _mapping(value: Any) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def summarize_evidence(artifact: dict[str, Any]) -> dict[str, Any]:
    evidence = _mapping(artifact.get("evidence"))
    provenance = _mapping(artifact.get("provenance"))
    sbom = _mapping(artifact.get("sbom"))
    checks = [
        {"name": "artifact_integrity", "passed": artifact.get("document_hash") == _hash(artifact)},
        {"name": "compliance_score", "passed": isinstance(artifact.get("score"), (int, float)),
         "value": artifact.get("score")},
        {"name": "attestation", "passed": bool(artifact.get("attestation_signed")),
         "value": bool(artifact.get("attestation_signed"))},
        {"name": "provenance", "passed": bool(provenance or evidence.get("provenance")),
         "value": provenance.get("builder_id") if provenance else None},
        {"name": "sbom", "passed": bool(sbom or evidence.get("sbom")),
         "value": sbom.get("component_count") if sbom else None},
        {"name": "clean_boot", "passed": bool(
            artifact.get("clean_boot_verified", evidence.get("clean_boot_verified", False)))},
        {"name": "critical_cves", "passed": int(
            artifact.get("critical_cves", evidence.get("critical_cves", 0)) or 0) == 0,
         "value": int(artifact.get("critical_cves", evidence.get("critical_cves", 0)) or 0)},
    ]
    return {
        "schema": EVIDENCE_SUMMARY_SCHEMA,
        "artifact_id": artifact.get("artifact_id"),
        "bucket": artifact.get("bucket"),
        "version": artifact.get("version"),
        "status": artifact.get("status"),
        "passed": sum(1 for check in checks if check["passed"]),
        "failed": sum(1 for check in checks if not check["passed"]),
        "checks": checks,
        "replicas": artifact.get("replicas", {}),
        "labels": artifact.get("labels", {}),
    }
