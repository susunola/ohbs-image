from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._logging import fail, ok
from ._registry import _hash, _read_object, _root, get_artifact
from ._reports import _atomic_write_bytes

POLICY_SCHEMA = "https://ohbs-image.dev/policy-bundle/v1"
DECISION_SCHEMA = "https://ohbs-image.dev/policy-decision/v1"
_CONTROL_NAMES = {"status", "attestation", "score", "freshness", "critical_cves"}
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def verify_policy_signature(path: Path, trusted_publishers: list[str], *,
                            signature: Path | None = None) -> str:
    signature_path = signature or path.with_suffix(path.suffix + ".asc")
    if not signature_path.is_file():
        raise ValueError(f"policy signature not found: {signature_path}")
    try:
        result = subprocess.run(
            ["gpg", "--batch", "--status-fd", "1", "--verify",
             str(signature_path), str(path)], capture_output=True, text=True,
            timeout=60, check=False)
    except FileNotFoundError as exc:
        raise ValueError("gpg not found; cannot verify required policy signature") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("policy signature verification timed out") from exc
    fingerprint = ""
    for line in result.stdout.splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fingerprint = line.split()[2].upper()
            break
    trusted = {item.replace(" ", "").upper() for item in trusted_publishers}
    if result.returncode != 0 or not fingerprint:
        raise ValueError("policy signature is invalid")
    if trusted and fingerprint not in trusted:
        raise ValueError(f"policy signer {fingerprint} is not a trusted publisher")
    return fingerprint


def _enforce_policy_trust(path: Path, doc: dict[str, Any]) -> str | None:
    trust = doc.get("trust")
    if not isinstance(trust, dict) or not trust.get("require_signature"):
        return None
    publishers = trust.get("trusted_publishers")
    if not isinstance(publishers, list) or not publishers:
        raise ValueError("trust.trusted_publishers is required when signatures are enforced")
    return verify_policy_signature(path, [str(item) for item in publishers])


def _merge(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result = dict(parent)
    for key, value in child.items():
        if key in {"defaults", "environments"} and isinstance(value, dict):
            current = result.get(key)
            merged = dict(current) if isinstance(current, dict) else {}
            for nested_key, nested_value in value.items():
                if key == "environments" and isinstance(nested_value, dict):
                    base = merged.get(nested_key)
                    merged[nested_key] = {**(base if isinstance(base, dict) else {}),
                                          **nested_value}
                else:
                    merged[nested_key] = nested_value
            result[key] = merged
        elif key == "exceptions" and isinstance(value, list):
            inherited = result.get(key)
            result[key] = ([*inherited, *value] if isinstance(inherited, list) else list(value))
        elif key != "extends":
            result[key] = value
    return result


def load_policy(path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    visited = set() if seen is None else seen
    if resolved in visited:
        raise ValueError(f"policy inheritance cycle at {resolved}")
    visited.add(resolved)
    doc = _read_object(resolved)
    if doc is None:
        raise ValueError(f"invalid policy bundle {resolved}")
    parent_name = doc.get("extends")
    if isinstance(parent_name, str) and parent_name:
        parent = load_policy((resolved.parent / parent_name), visited)
        doc = _merge(parent, doc)
    return doc


def verify_policy(doc: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if doc.get("schema") != POLICY_SCHEMA:
        failures.append("schema mismatch")
    if not _SAFE_NAME.fullmatch(str(doc.get("policy_id") or "")):
        failures.append("policy_id is missing or unsafe")
    defaults = doc.get("defaults")
    if not isinstance(defaults, dict):
        failures.append("defaults must be an object")
        defaults = {}
    environments = doc.get("environments")
    if not isinstance(environments, dict):
        failures.append("environments must be an object")
        environments = {}
    for scope, rules in [("defaults", defaults), *[
            (f"environments.{name}", value)
            for name, value in environments.items()
            if isinstance(value, dict)]]:
        score = rules.get("min_score")
        if score is not None and (not isinstance(score, (int, float)) or not 0 <= score <= 100):
            failures.append(f"{scope}.min_score must be 0-100")
        age = rules.get("max_age_days")
        if age is not None and (not isinstance(age, int) or age < 0):
            failures.append(f"{scope}.max_age_days must be a non-negative integer")
    exceptions = doc.get("exceptions", [])
    if not isinstance(exceptions, list):
        failures.append("exceptions must be an array")
        return failures
    seen_ids: set[str] = set()
    trust = doc.get("trust")
    trusted_approvers = ({str(item) for item in trust.get("trusted_approvers", [])}
                         if isinstance(trust, dict) else set())
    separate = bool(trust.get("enforce_separation")) if isinstance(trust, dict) else False
    for index, item in enumerate(exceptions):
        prefix = f"exceptions[{index}]"
        if not isinstance(item, dict):
            failures.append(f"{prefix} must be an object")
            continue
        exception_id = str(item.get("id") or "")
        if not _SAFE_NAME.fullmatch(exception_id) or exception_id in seen_ids:
            failures.append(f"{prefix}.id is missing, unsafe, or duplicated")
        seen_ids.add(exception_id)
        for field in ("owner", "approved_by", "reason", "expires_at"):
            if not str(item.get(field) or "").strip():
                failures.append(f"{prefix}.{field} is required")
        owner, approver = str(item.get("owner") or ""), str(item.get("approved_by") or "")
        if trusted_approvers and approver not in trusted_approvers:
            failures.append(f"{prefix}.approved_by is not a trusted approver")
        if separate and owner == approver:
            failures.append(f"{prefix} violates owner/approver separation")
        controls = item.get("controls")
        if not isinstance(controls, list) or not controls or not set(controls) <= _CONTROL_NAMES:
            failures.append(f"{prefix}.controls contains unknown or missing controls")
        try:
            datetime.fromisoformat(str(item.get("expires_at")).replace("Z", "+00:00"))
        except ValueError:
            failures.append(f"{prefix}.expires_at must be ISO-8601")
    return failures


def _rules_for(doc: dict[str, Any], environment: str) -> dict[str, Any]:
    rules = dict(doc.get("defaults") or {})
    environments = doc.get("environments")
    override = environments.get(environment) if isinstance(environments, dict) else None
    if isinstance(override, dict):
        rules.update(override)
    return rules


def _active_exceptions(doc: dict[str, Any], artifact_id: str, environment: str,
                       now: datetime) -> dict[str, dict[str, Any]]:
    waived: dict[str, dict[str, Any]] = {}
    for item in doc.get("exceptions", []):
        if not isinstance(item, dict):
            continue
        if item.get("artifact_id") not in {None, "", artifact_id}:
            continue
        if item.get("environment") not in {None, "", environment}:
            continue
        try:
            expires = datetime.fromisoformat(str(item.get("expires_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires <= now:
            continue
        for control in item.get("controls", []):
            waived[str(control)] = item
    return waived


def evaluate_policy(doc: dict[str, Any], artifact: dict[str, Any], environment: str, *,
                    now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    artifact_id = str(artifact.get("artifact_id") or "")
    rules = _rules_for(doc, environment)
    waived = _active_exceptions(doc, artifact_id, environment, current)
    checks: list[dict[str, Any]] = []

    def check(control: str, passed: bool, actual: Any, required: Any) -> None:
        exception = waived.get(control) if not passed else None
        exception_id = exception.get("id") if isinstance(exception, dict) else None
        checks.append({"control": control,
                       "result": "exception" if exception else ("pass" if passed else "deny"),
                       "actual": actual, "required": required,
                       "exception_id": exception_id})

    allowed = rules.get("allowed_status", ["active"])
    check("status", artifact.get("status") in allowed, artifact.get("status"), allowed)
    required_signed = bool(rules.get("require_attestation", False))
    check("attestation", not required_signed or bool(artifact.get("attestation_signed")),
          bool(artifact.get("attestation_signed")), required_signed)
    min_score = float(rules.get("min_score", 0))
    score = artifact.get("score")
    check("score", min_score == 0 or (isinstance(score, (int, float)) and float(score) >= min_score),
          score, min_score)
    max_age = int(rules.get("max_age_days", 0))
    try:
        created = datetime.fromisoformat(str(artifact.get("created_at")).replace("Z", "+00:00"))
        age_days = max(0, (current - created).days)
    except ValueError:
        age_days = None
    check("freshness", max_age == 0 or (age_days is not None and age_days <= max_age),
          age_days, max_age)
    raw_evidence = artifact.get("evidence")
    evidence: dict[str, Any] = raw_evidence if isinstance(raw_evidence, dict) else {}
    critical = artifact.get("critical_cves", evidence.get("critical_cves", 0))
    max_critical = int(rules.get("max_critical_cves", 0))
    check("critical_cves", isinstance(critical, int) and critical <= max_critical,
          critical, max_critical)
    decision: dict[str, Any] = {
        "schema": DECISION_SCHEMA, "policy_id": doc.get("policy_id"),
        "policy_version": doc.get("version"), "artifact_id": artifact_id,
        "environment": environment, "evaluated_at": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allowed": not any(item["result"] == "deny" for item in checks), "checks": checks,
    }
    decision["document_hash"] = _hash(decision)
    return decision


def record_decision(decision: dict[str, Any], root: Path | None = None) -> Path:
    stamp = str(decision["evaluated_at"]).replace(":", "").replace("-", "")
    digest = str(decision.get("document_hash") or "")[:12]
    name = f"{stamp}-{decision['artifact_id']}-{decision['environment']}-{digest}.json"
    path = _root(root) / "decisions" / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write_bytes(path, (json.dumps(decision, ensure_ascii=False, indent=2) + "\n").encode())
    return path


def check_artifact(policy_path: Path, artifact_id: str, environment: str,
                   root: Path | None = None) -> dict[str, Any]:
    doc = load_policy(policy_path)
    failures = verify_policy(doc)
    if failures:
        raise ValueError("invalid policy: " + "; ".join(failures))
    signer = _enforce_policy_trust(policy_path.resolve(), doc)
    artifact = get_artifact(artifact_id, root)
    if artifact is None or artifact.get("document_hash") != _hash(artifact):
        raise ValueError(f"artifact {artifact_id} not found or failed integrity verification")
    decision = evaluate_policy(doc, artifact, environment)
    if signer:
        decision["policy_signer_fingerprint"] = signer
        decision["document_hash"] = _hash(decision)
    record_decision(decision, root)
    return decision


def cmd_policy_verify(args: argparse.Namespace) -> int:
    try:
        bundle = Path(args.bundle)
        doc = load_policy(bundle)
        failures = verify_policy(doc)
        if not failures:
            _enforce_policy_trust(bundle.resolve(), doc)
    except (OSError, ValueError) as exc:
        failures = [str(exc)]
    if args.output == "json":
        print(json.dumps({"valid": not failures, "failures": failures}, indent=2))
    elif failures:
        for message in failures:
            fail(message)
    else:
        ok("Policy bundle and trust requirements are valid")
    return 1 if failures else 0


def cmd_policy_check(args: argparse.Namespace) -> int:
    try:
        decision = check_artifact(Path(args.bundle), args.artifact_id, args.environment)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 2
    if args.output == "json":
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    else:
        for item in decision["checks"]:
            print(f"{item['result']:9s} {item['control']}: {item['actual']} (required {item['required']})")
    return 0 if decision["allowed"] else 1
