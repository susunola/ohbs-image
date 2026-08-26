from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ._policy import evaluate_policy, verify_policy
from ._registry import _hash

POLICY_DIFF_SCHEMA = "https://ohbs-image.dev/policy-diff/v1"
EXCEPTION_PREVIEW_SCHEMA = "https://ohbs-image.dev/policy-exception-preview/v1"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(nested, path))
        return result
    return {prefix: value}


def diff_policy(candidate: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    failures = verify_policy(candidate)
    if failures:
        raise ValueError("invalid candidate policy: " + "; ".join(failures))
    before = _flatten(baseline or {})
    after = _flatten(candidate)
    changes = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        kind = "added" if path not in before else "removed" if path not in after else "changed"
        changes.append({"path": path, "kind": kind, "before": old, "after": new})
    result: dict[str, Any] = {
        "schema": POLICY_DIFF_SCHEMA,
        "policy_id": candidate.get("policy_id"),
        "baseline_version": baseline.get("version") if baseline else None,
        "candidate_version": candidate.get("version"),
        "change_count": len(changes),
        "changes": changes,
    }
    result["document_hash"] = _hash(result)
    return result


def preview_exceptions(bundle: dict[str, Any], artifacts: list[dict[str, Any]],
                       environment: str, *, now: datetime | None = None,
                       warning_days: int = 30) -> dict[str, Any]:
    failures = verify_policy(bundle)
    if failures:
        raise ValueError("invalid policy: " + "; ".join(failures))
    current = now or datetime.now(UTC)
    rows = []
    for item in bundle.get("exceptions", []):
        expires = datetime.fromisoformat(str(item["expires_at"]).replace("Z", "+00:00"))
        days_remaining = (expires - current).days
        without = {**bundle, "exceptions": [entry for entry in bundle.get("exceptions", [])
                                             if entry.get("id") != item.get("id")]}
        newly_denied = []
        for artifact in artifacts:
            before = evaluate_policy(bundle, artifact, environment, now=current)
            after = evaluate_policy(without, artifact, environment, now=current)
            if before["allowed"] and not after["allowed"]:
                newly_denied.append(str(artifact.get("artifact_id") or ""))
        rows.append({
            "id": item.get("id"), "owner": item.get("owner"),
            "approved_by": item.get("approved_by"), "expires_at": item.get("expires_at"),
            "days_remaining": days_remaining,
            "status": "expired" if days_remaining < 0 else (
                "expiring" if days_remaining <= warning_days else "active"),
            "controls": item.get("controls", []),
            "affected_artifact_count": len(newly_denied),
            "affected_artifacts": newly_denied,
        })
    rows.sort(key=lambda row: int(row["days_remaining"]))
    result: dict[str, Any] = {
        "schema": EXCEPTION_PREVIEW_SCHEMA,
        "policy_id": bundle.get("policy_id"), "environment": environment,
        "warning_days": warning_days, "exception_count": len(rows), "exceptions": rows,
        "dry_run": True,
    }
    result["document_hash"] = _hash(result)
    return result
