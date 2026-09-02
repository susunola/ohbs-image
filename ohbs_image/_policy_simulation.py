from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import fail
from ._policy import _enforce_policy_trust, evaluate_policy, load_policy, verify_policy
from ._registry import _hash, collect_artifacts, get_artifact

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
    # The result carries a document_hash, so iteration order is part of the
    # contract. Registry reads (SQLite or a filesystem glob) do not guarantee a
    # stable order, which would make two runs over identical inputs disagree.
    for artifact in sorted(artifacts, key=lambda item: str(item.get("artifact_id") or "")):
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


def _load_checked(path: Path) -> dict[str, Any]:
    """Load and validate one policy bundle, enforcing its trust requirements."""
    document = load_policy(path)
    failures = verify_policy(document)
    if failures:
        raise ValueError(f"invalid policy {path}: " + "; ".join(failures))
    _enforce_policy_trust(path.resolve(), document)
    return document


def cmd_policy_simulate(args: argparse.Namespace) -> int:
    try:
        candidate = _load_checked(Path(args.bundle))
        baseline = _load_checked(Path(args.baseline)) if args.baseline else None
        if args.artifact:
            artifacts: list[dict[str, Any]] = []
            for artifact_id in args.artifact:
                document = get_artifact(artifact_id)
                if document is None or document.get("document_hash") != _hash(document):
                    raise ValueError(
                        f"artifact {artifact_id} not found or failed integrity verification")
                artifacts.append(document)
        else:
            artifacts = collect_artifacts()
        if not artifacts:
            raise ValueError("no registered artifacts to simulate against")
        result = simulate_policy(candidate, artifacts, args.environment, baseline=baseline)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Policy {result['policy_id']}@{result['policy_version']} -> "
              f"{result['environment']} (dry run)")
        if result["baseline_policy_id"]:
            print(f"Baseline {result['baseline_policy_id']}"
                  f"@{result['baseline_policy_version']}")
        for row in result["artifacts"]:
            marker = {"unchanged": "=", "uncompared": " ",
                      "newly_allowed": "+", "newly_denied": "-"}[row["transition"]]
            verdict = "allow" if row["after_allowed"] else "deny "
            denied = ",".join(str(name) for name in row["denied_controls"])
            suffix = f"  denied: {denied}" if denied else ""
            print(f"  {marker} {row['artifact_id']:<24s} {verdict}{suffix}")
        summary = result["summary"]
        compared = "" if baseline is None else (
            f", {summary['newly_allowed']} newly allowed, "
            f"{summary['newly_denied']} newly denied")
        print(f"{summary['allowed']} allowed, {summary['denied']} denied"
              f"{compared} of {result['artifact_count']} artifact(s)")
        if result["control_denials"]:
            top = ", ".join(f"{name} ({count})"
                            for name, count in list(result["control_denials"].items())[:5])
            print(f"top denied controls: {top}")
    if args.fail_on_newly_denied and result["summary"]["newly_denied"]:
        return 1
    return 0
