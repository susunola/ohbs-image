from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from ._policy import evaluate_policy, verify_policy
from ._registry import _hash

POLICY_SIMULATION_SCHEMA = "https://ohbs-image.dev/policy-simulation/v1"


def simulate_policy(candidate: dict[str, Any], artifacts: list[dict[str, Any]],
                    environment: str, *, baseline: dict[str, Any] | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    failures = verify_policy(candidate)
    if failures:
        raise ValueError("invalid candidate policy: " + "; ".join(failures))
    if baseline is not None:
        baseline_failures = verify_policy(baseline)
        if baseline_failures:
            raise ValueError("invalid baseline policy: " + "; ".join(baseline_failures))
    current = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    control_denials: Counter[str] = Counter()
    allowed = denied = newly_allowed = newly_denied = unchanged = 0
    for artifact in artifacts:
        decision = evaluate_policy(candidate, artifact, environment, now=current)
        before = (evaluate_policy(baseline, artifact, environment, now=current)["allowed"]
                  if baseline is not None else None)
        after = bool(decision["allowed"])
        allowed += int(after)
        denied += int(not after)
        if before is None or before == after:
            unchanged += 1
            transition = "unchanged" if before is not None else "uncompared"
        elif after:
            newly_allowed += 1
            transition = "newly_allowed"
        else:
            newly_denied += 1
            transition = "newly_denied"
        failed_controls = [str(check["control"]) for check in decision["checks"]
                           if check["result"] == "deny"]
        control_denials.update(failed_controls)
        rows.append({
            "artifact_id": artifact.get("artifact_id"),
            "bucket": artifact.get("bucket"),
            "version": artifact.get("version"),
            "before_allowed": before,
            "after_allowed": after,
            "transition": transition,
            "denied_controls": failed_controls,
        })
    result: dict[str, Any] = {
        "schema": POLICY_SIMULATION_SCHEMA,
        "policy_id": candidate.get("policy_id"),
        "policy_version": candidate.get("version"),
        "baseline_policy_id": baseline.get("policy_id") if baseline else None,
        "baseline_policy_version": baseline.get("version") if baseline else None,
        "environment": environment,
        "evaluated_at": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifact_count": len(rows),
        "summary": {
            "allowed": allowed,
            "denied": denied,
            "newly_allowed": newly_allowed,
            "newly_denied": newly_denied,
            "unchanged": unchanged,
        },
        "control_denials": dict(sorted(control_denials.items())),
        "artifacts": rows,
        "dry_run": True,
    }
    result["document_hash"] = _hash(result)
    return result
