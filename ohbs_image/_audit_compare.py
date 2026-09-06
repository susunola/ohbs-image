"""Rule-level comparison between OHBS audit JSON and independent audit evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ._logging import ConfigError, fail
from ._reports import _atomic_write_bytes

COMPARE_SCHEMA = "https://ohbs-image.dev/audit-comparison/v1"
_OHBS_XCCDF_PREFIX = "xccdf_org.ohbs_image.content_rule_"


def _canonical_id(value: object) -> str:
    text = str(value or "").strip()
    return text[len(_OHBS_XCCDF_PREFIX):] if text.startswith(_OHBS_XCCDF_PREFIX) else text


def _status(value: object) -> str:
    token = re.sub(r"[^a-z]", "", str(value or "").lower())
    aliases = {
        "pass": "pass", "passed": "pass",
        "fixed": "not_evaluated", "applied": "not_evaluated",
        "fail": "fail", "failed": "fail",
        "error": "error", "manual": "manual", "informational": "manual",
        "notapplicable": "not_applicable", "na": "not_applicable",
        "notselected": "not_evaluated", "notevaluated": "not_evaluated",
        "skipped": "not_evaluated", "unknown": "unknown",
    }
    return aliases.get(token, "unknown")


def _json_audit(data: dict[str, Any]) -> tuple[str, dict[str, str]]:
    benchmark = str(data.get("benchmark") or data.get("benchmark_reference") or
                    (data.get("metadata") or {}).get("benchmark") or "")
    results: dict[str, str] = {}
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        rule_id = _canonical_id(item.get("id", item.get("rule_id")))
        if not rule_id:
            continue
        if rule_id in results:
            raise ConfigError(f"audit input contains duplicate rule ID {rule_id}")
        results[rule_id] = _status(item.get("status", item.get("result")))
    return benchmark, results


def _xccdf_audit(content: bytes) -> tuple[str, dict[str, str]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ConfigError(f"invalid XCCDF/XML audit input: {exc}") from exc
    test_results = root.findall(".//{*}TestResult")
    current = test_results[-1] if test_results else None
    if current is None:
        raise ConfigError("XCCDF/XML input contains no TestResult")
    root_is_benchmark = root.tag.rsplit("}", 1)[-1] == "Benchmark"
    benchmark = str(current.get("benchmark-reference") or
                    (root.get("id") if root_is_benchmark else "") or "")
    results: dict[str, str] = {}
    for item in current.findall("{*}rule-result"):
        rule_id = _canonical_id(item.get("idref"))
        node = item.find("{*}result")
        if not rule_id:
            continue
        if rule_id in results:
            raise ConfigError(f"audit input contains duplicate rule ID {rule_id}")
        results[rule_id] = _status(node.text if node is not None else "unknown")
    return benchmark, results


def load_audit(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        benchmark, results = _xccdf_audit(content)
        format_name = "xccdf"
    else:
        if not isinstance(decoded, dict):
            raise ConfigError("JSON audit input must be an object")
        benchmark, results = _json_audit(decoded)
        format_name = "json"
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
            "format": format_name, "benchmark": benchmark, "results": results}


def compare_audits(internal: dict[str, Any], external: dict[str, Any],
                   *, min_overlap_percent: float = 0.0) -> dict[str, Any]:
    if not 0 <= min_overlap_percent <= 100:
        raise ConfigError("minimum overlap percent must be between 0 and 100")
    left = internal["results"]
    right = external["results"]
    shared = sorted(set(left) & set(right))
    union = set(left) | set(right)
    overlap = 100.0 if not union else 100.0 * len(shared) / len(union)
    agreements: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    for rule_id in shared:
        row = {"rule_id": rule_id, "internal": left[rule_id], "external": right[rule_id]}
        if "unknown" in (left[rule_id], right[rule_id]):
            unknown.append(row)
        elif left[rule_id] == right[rule_id]:
            agreements.append(row)
        else:
            conflicts.append(row)
    ib = str(internal.get("benchmark") or "").strip().lower()
    eb = str(external.get("benchmark") or "").strip().lower()
    benchmark_mismatch = bool(ib and eb and ib != eb)
    passed = bool(shared) and bool(ib and eb) and not benchmark_mismatch \
        and not conflicts and not unknown and overlap >= min_overlap_percent \
        and all(left[rule_id] not in {"not_evaluated", "error"} for rule_id in shared)
    return {
        "schema": COMPARE_SCHEMA, "passed": passed,
        "benchmark_mismatch": benchmark_mismatch,
        "internal": {key: value for key, value in internal.items() if key != "results"},
        "external": {key: value for key, value in external.items() if key != "results"},
        "summary": {"internal_rules": len(left), "external_rules": len(right),
                    "shared_rules": len(shared), "agreements": len(agreements),
                    "conflicts": len(conflicts), "unknown_status": len(unknown),
                    "overlap_percent": round(overlap, 3),
                    "min_overlap_percent": min_overlap_percent},
        "agreements": agreements, "conflicts": conflicts, "unknown_status": unknown,
        "only_internal": sorted(set(left) - set(right)),
        "only_external": sorted(set(right) - set(left)),
    }


def cmd_native_audit_compare(args: argparse.Namespace) -> int:
    try:
        result = compare_audits(
            load_audit(Path(args.internal)), load_audit(Path(args.external)),
            min_overlap_percent=args.min_overlap_percent)
    except (OSError, ConfigError) as exc:
        fail(str(exc))
        return 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        _atomic_write_bytes(Path(args.output), (rendered + "\n").encode())
    print(rendered)
    return 0 if result["passed"] else 1
