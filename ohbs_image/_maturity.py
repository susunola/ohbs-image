from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ._accuracy_baseline import verify_baseline

MATURITY_SCHEMA = "https://ohbs-image.dev/tencentcloud-production-maturity/v1"


def evaluate_maturity(*, baseline: dict[str, Any], phase: dict[str, Any],
                      release: dict[str, Any], sweep: dict[str, Any],
                      fault_matrix: dict[str, Any]) -> dict[str, Any]:
    baseline_check = verify_baseline(baseline)
    checks = [
        {"id": "accuracy-baseline", "passed": bool(baseline_check["valid"]),
         "evidence": baseline.get("document_sha256", ""),
         "detail": "; ".join(baseline_check["failures"]) or "all content hashes verified"},
        {"id": "phase-performance", "passed": phase.get("passed") is True,
         "evidence": phase.get("schema", ""), "detail": "all P95 phase budgets satisfied"},
        {"id": "clean-boot", "passed": release.get("clean_boot_verified") is True,
         "evidence": release.get("clean_boot_evidence", ""),
         "detail": "produced image was re-audited from a fresh instance"},
        {"id": "signed-attestation", "passed": release.get("signed") is True,
         "evidence": release.get("provenance", ""), "detail": "release provenance is signed"},
        {"id": "rollback", "passed": release.get("rollback_verified") is True,
         "evidence": release.get("channel_evidence", ""),
         "detail": "stable channel rollback was exercised"},
        {"id": "resource-leases", "passed": int(sweep.get("failed") or 0) == 0,
         "evidence": sweep.get("evidence_path", ""),
         "detail": f"expired resources observed: {int(sweep.get('resources') or 0)}"},
        {"id": "fault-matrix", "passed": fault_matrix.get("passed") is True,
         "evidence": fault_matrix.get("evidence_path", ""),
         "detail": "release failure injection matrix passed"},
        {"id": "documentation", "passed": release.get("compatibility_documented") is True,
         "evidence": release.get("compatibility_evidence", ""),
         "detail": "Native/Packer compatibility is generated and current"},
    ]
    failed = [check["id"] for check in checks if not check["passed"]]
    return {"schema": MATURITY_SCHEMA, "provider": "tencentcloud",
            "production_ready": not failed, "checks": checks,
            "summary": {"passed": len(checks) - len(failed), "failed": len(failed),
                        "total": len(checks)}, "failed_checks": failed}


def cmd_proof_maturity(args: argparse.Namespace) -> int:
    def load(path: str) -> dict[str, Any]:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return value

    result = evaluate_maturity(
        baseline=load(args.baseline), phase=load(args.phase),
        release=load(args.release), sweep=load(args.sweep),
        fault_matrix=load(args.fault_matrix))
    rendered = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["production_ready"] else 3
