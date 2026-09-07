from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE_SCHEMA = "https://ohbs-image.dev/accuracy-baseline/v1"
MATRIX_SCHEMA = "https://ohbs-image.dev/golden-matrix/v1"
COUNT_KEYS = ("pass", "fail", "manual", "not_scored", "not_applicable", "error", "skip")


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_matrix(document: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if document.get("schema") != MATRIX_SCHEMA:
        failures.append("unsupported matrix schema")
    rows = document.get("targets")
    if not isinstance(rows, list) or not rows:
        return failures + ["targets must be a non-empty list"]
    seen: set[tuple[str, int]] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            failures.append(f"target {index} must be an object")
            continue
        label = f"target {index}"
        profile = str(row.get("profile") or "")
        level = row.get("level")
        key = (profile, level) if isinstance(level, int) else (profile, -1)
        if not profile:
            failures.append(f"{label}: missing profile")
        if level not in (1, 2):
            failures.append(f"{label}: level must be 1 or 2")
        if key in seen:
            failures.append(f"{label}: duplicate profile/level {profile}/L{level}")
        seen.add(key)
        for name in ("source_image_id", "region", "zone", "instance_type", "benchmark"):
            if not str(row.get(name) or "").strip():
                failures.append(f"{label}: missing {name}")
        source = str(row.get("source_image_id") or "")
        if source and not source.startswith("img-"):
            failures.append(f"{label}: invalid source_image_id")
    return failures


def coverage_counts(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = dict.fromkeys(COUNT_KEYS, 0)
    aliases = {"notapplicable": "not_applicable", "not-selected": "not_scored",
               "notselected": "not_scored", "not_selected": "not_scored",
               "informational": "manual", "na": "not_applicable", "skipped": "skip"}
    unknown: dict[str, int] = {}
    for result in results:
        raw = str(result.get("status") or "error").strip().lower().replace(" ", "_")
        status = aliases.get(raw, raw)
        if status in counts:
            counts[status] += 1
        else:
            unknown[status] = unknown.get(status, 0) + 1
            counts["error"] += 1
    applicable = counts["pass"] + counts["fail"] + counts["error"]
    score = round(100 * counts["pass"] / applicable, 3) if applicable else None
    return {**counts, "applicable": applicable, "total": len(results),
            "coverage_percent": score, "audit_pass_percent": score,
            "unknown_statuses": unknown,
            "formula": "pass / (pass + fail + error) * 100"}


def explain_gaps(results: list[dict[str, Any]],
                 rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Classify every non-pass without pretending that exclusions are success."""
    catalog = {str(rule.get("id") or ""): rule for rule in (rules or [])}
    categories = {name: [] for name in ("true_failure", "detector_error", "pending_reboot",
                  "environment_limited", "manual", "not_applicable", "not_scored",
                  "implementation_missing")}
    seen: set[str] = set()
    for result in results:
        rule_id = str(result.get("id") or "")
        if rule_id:
            seen.add(rule_id)
        status = str(result.get("status") or "error").lower().replace(" ", "_")
        apply_status = str(result.get("apply_status") or "").lower()
        reason = str(result.get("reason") or result.get("detail") or "")
        if status == "pass":
            continue
        if apply_status == "applied_pending" or bool(result.get("requires_reboot")):
            category = "pending_reboot"
        elif apply_status in {"unsupported", "environment_limited"}:
            category = "environment_limited"
        elif status in {"manual", "informational"}:
            category = "manual"
        elif status in {"notapplicable", "not_applicable", "na"}:
            category = "not_applicable"
        elif status in {"notselected", "not_selected", "not_scored", "skipped"}:
            category = "not_scored"
        elif status == "error":
            category = "detector_error"
        else:
            category = "true_failure"
        categories[category].append({"id": rule_id, "status": status, "reason": reason,
                                     "title": str((catalog.get(rule_id) or {}).get("title") or
                                                  result.get("title") or "")})
    for rule_id, rule in catalog.items():
        if rule_id not in seen:
            categories["implementation_missing"].append({"id": rule_id, "status": "missing",
                "reason": "catalog rule has no audit result", "title": str(rule.get("title") or "")})
    counts = coverage_counts(results)
    deduction = round(100.0 - float(counts["coverage_percent"]), 3) \
        if counts["coverage_percent"] is not None else None
    missing = sorted(set(catalog) - seen)
    frequencies = Counter(str(row.get("id") or "") for row in results)
    duplicates = sorted(rule_id for rule_id, count in frequencies.items() if count > 1)
    unexpected = sorted(seen - set(catalog)) if rules is not None else []
    complete = bool(catalog) and not missing and not duplicates and not unexpected \
        and all(str(row.get("id") or "") for row in results)
    return {"coverage": counts, "deduction_percent": deduction,
            "evidence": {"catalog_supplied": rules is not None,
                         "complete": complete, "missing_rules": missing,
                         "duplicate_rules": duplicates, "unexpected_rules": unexpected,
                         "result_coverage_percent": round(100 * len(seen & set(catalog)) /
                                                          len(catalog), 3) if catalog else None},
            "categories": categories,
            "category_counts": {key: len(value) for key, value in categories.items()}}


def rule_ledger(role_dir: Path, *, benchmark: str) -> dict[str, Any]:
    rules_path = role_dir / "files" / "rules.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    rows = []
    for rule in rules:
        rows.append({
            "id": str(rule.get("id") or ""), "title": str(rule.get("title") or ""),
            "benchmark": benchmark, "section": str(rule.get("section") or ""),
            "page": rule.get("page"), "assessment": str(rule.get("assessment") or ""),
            "family": str(rule.get("family") or ""), "levels": rule.get("levels") or [],
            "automation": "manual" if str(rule.get("assessment") or "").lower() == "manual"
            else "automated", "implementation": "bundled",
        })
    return {"benchmark": benchmark, "role": role_dir.name, "rules": rows,
            "rules_sha256": canonical_sha256(rows)}


def summarize_repeats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if len(runs) < 2:
        raise ValueError("at least two runs are required to measure variance")
    scores = [float(run["coverage"]["coverage_percent"]) for run in runs
              if (run.get("coverage") or {}).get("coverage_percent") is not None]
    if len(scores) != len(runs):
        raise ValueError("every run must have a numeric coverage_percent")
    durations: dict[str, list[float]] = {}
    for run in runs:
        for phase, value in (run.get("phase_duration_seconds") or {}).items():
            durations.setdefault(str(phase), []).append(float(value))
    phases = {phase: {"samples": len(values), "p50_seconds": round(statistics.median(values), 3),
                      "p95_seconds": round(sorted(values)[max(0, (95 * len(values) + 99) // 100 - 1)], 3)}
              for phase, values in sorted(durations.items())}
    statuses: dict[str, set[str]] = {}
    for run in runs:
        for result in run.get("results") or []:
            statuses.setdefault(str(result.get("id") or ""), set()).add(
                str(result.get("status") or "error"))
    unstable = sorted(rule for rule, values in statuses.items() if len(values) > 1)
    return {"runs": len(runs), "score": {"min": min(scores), "max": max(scores),
            "mean": round(statistics.mean(scores), 3),
            "population_stddev": round(statistics.pstdev(scores), 3)},
            "unstable_rules": unstable, "phase_duration": phases}


def create_baseline(matrix: dict[str, Any], run_documents: list[dict[str, Any]],
                    ledgers: list[dict[str, Any]]) -> dict[str, Any]:
    failures = validate_matrix(matrix)
    if failures:
        raise ValueError("; ".join(failures))
    inputs = {"matrix": matrix, "runs": run_documents, "rule_ledgers": ledgers}
    manifest: dict[str, Any] = {
        "schema": BASELINE_SCHEMA,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matrix_sha256": canonical_sha256(matrix),
        "run_sha256": [canonical_sha256(run) for run in run_documents],
        "rule_ledger_sha256": [canonical_sha256(ledger) for ledger in ledgers],
        "inputs": inputs,
    }
    manifest["document_sha256"] = canonical_sha256(manifest)
    return manifest


def verify_baseline(document: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    if document.get("schema") != BASELINE_SCHEMA:
        failures.append("unsupported baseline schema")
    expected = str(document.get("document_sha256") or "")
    unsigned = {key: value for key, value in document.items() if key != "document_sha256"}
    if expected != canonical_sha256(unsigned):
        failures.append("document_sha256 mismatch")
    inputs = document.get("inputs") or {}
    if str(document.get("matrix_sha256") or "") != canonical_sha256(inputs.get("matrix")):
        failures.append("matrix_sha256 mismatch")
    runs = inputs.get("runs") or []
    if document.get("run_sha256") != [canonical_sha256(run) for run in runs]:
        failures.append("run_sha256 mismatch")
    ledgers = inputs.get("rule_ledgers") or []
    if document.get("rule_ledger_sha256") != [canonical_sha256(row) for row in ledgers]:
        failures.append("rule_ledger_sha256 mismatch")
    return {"valid": not failures, "failures": failures,
            "matrix_targets": len((inputs.get("matrix") or {}).get("targets") or []),
            "runs": len(runs), "rule_ledgers": len(ledgers)}


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_baseline_create(args: argparse.Namespace) -> int:
    document = create_baseline(_load(args.matrix), [_load(path) for path in args.run],
                               [_load(path) for path in args.rule_ledger])
    Path(args.output).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    print(json.dumps(verify_baseline(document), indent=2))
    return 0


def cmd_baseline_verify(args: argparse.Namespace) -> int:
    result = verify_baseline(_load(args.baseline))
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


def cmd_baseline_ledger(args: argparse.Namespace) -> int:
    from ._profiles import PROFILES
    profile = PROFILES[args.profile]
    role = Path(__file__).parent / "roles" / str(profile["role_dir"])
    document = rule_ledger(role, benchmark=str(profile["benchmark"]))
    Path(args.output).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    print(json.dumps({"profile": args.profile, "rules": len(document["rules"]),
                      "rules_sha256": document["rules_sha256"]}, indent=2))
    return 0


def cmd_baseline_summarize(args: argparse.Namespace) -> int:
    document = summarize_repeats([_load(path) for path in args.run])
    if args.output:
        Path(args.output).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0


def cmd_baseline_explain(args: argparse.Namespace) -> int:
    audit = _load(args.audit)
    results = audit.get("results") if isinstance(audit, dict) else None
    if not isinstance(results, list):
        raise ValueError("audit document must contain a results list")
    rules: list[dict[str, Any]] | None = None
    if args.rule_ledger:
        ledger = _load(args.rule_ledger)
        if isinstance(ledger, dict) and isinstance(ledger.get("rules"), list):
            rules = ledger["rules"]
    document = explain_gaps([row for row in results if isinstance(row, dict)], rules)
    if args.output:
        Path(args.output).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0
