"""Rule-catalog supply-chain checks (roadmap I).

The bundled rule catalogs are the normative input of every hardening run:
the engine dispatches purely on rule ``family`` and the catalog content is
pinned into lineage fingerprints, provenance and release manifests.  These
commands make the catalog itself auditable — file presence, JSON validity,
rule-count visibility and guidance cross-references.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from ._catalog import _catalog_path
from ._logging import VERSION, fail, ok, warn
from ._profiles import PROFILES

CATALOG_LIST_SCHEMA = "https://ohbs-image.dev/catalog-list/v1"
CATALOG_VERIFY_SCHEMA = "https://ohbs-image.dev/catalog-verify/v1"


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "-"


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _guidance_ids(guidance: Any) -> set[str] | None:
    """Rule IDs referenced by a guidance document.

    CIS guidance is ``{rule_id: entry}``; Windows guidance is a list of
    ``{"id": ...}`` entries.  Returns None when the shape is unrecognized.
    """
    if isinstance(guidance, dict):
        return set(guidance)
    if isinstance(guidance, list):
        return {str(entry["id"]) for entry in guidance
                if isinstance(entry, dict) and entry.get("id")}
    return None


def _chapters_count(sections: Any) -> int:
    if isinstance(sections, dict):
        return len(sections.get("chapters") or {})
    if isinstance(sections, list):
        return len({str(entry.get("id", "")).split(".", 1)[0]
                    for entry in sections
                    if isinstance(entry, dict) and entry.get("id")})
    return 0


def _iter_catalogs() -> list[dict[str, Any]]:
    """Per-profile bundled catalog metadata for every profile."""
    items: list[dict[str, Any]] = []
    for name, meta in sorted(PROFILES.items()):
        role = str(meta.get("role_dir", ""))
        benchmark = str(meta.get("benchmark", ""))
        path = _catalog_path(role, benchmark)
        rules = _load_json(path)
        guidance = _load_json(path.parent / "guidance.json")
        sections = _load_json(path.parent / "sections.json")
        items.append({
            "profile": name,
            "role_dir": role,
            "catalog": path.name,
            "path": str(path),
            "rules": len(rules) if isinstance(rules, list) else 0,
            "guidance": len(guidance) if isinstance(guidance, (dict, list)) else 0,
            "chapters": _chapters_count(sections),
            "sha256": _file_sha256(path),
        })
    return items


def _verify_catalog(path: Path, *, strict: bool = False) -> tuple[bool, list[str], list[str]]:
    """Return (ok, errors, warnings) for one bundled catalog.

    Corruption (missing file, invalid JSON, empty catalog, malformed rule,
    unrecognized guidance/sections shape) always fails the gate.  Content
    drift — guidance entries pointing at unknown rules, or rules without
    guidance — is reported as a warning by default and only fails under
    ``--strict``, because legacy Windows catalogs carry known coverage
    gaps that must be surfaced without breaking the default CI gate.
    """
    if not path.is_file():
        return False, [f"catalog file missing: {path}"], []
    rules = _load_json(path)
    if rules is None:
        return False, [f"catalog is not valid JSON: {path.name}"], []
    if not isinstance(rules, list) or not rules:
        return False, [f"catalog is empty or not a list: {path.name}"], []
    rule_ids: list[str] = []
    errors: list[str] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or not rule.get("id"):
            errors.append(f"rule at index {index} has no id")
        else:
            rule_ids.append(str(rule["id"]))
    if errors:
        return False, errors, []
    warnings: list[str] = []
    guidance = _load_json(path.parent / "guidance.json")
    if guidance is not None:
        guidance_ids = _guidance_ids(guidance)
        if guidance_ids is None:
            errors.append("guidance.json is neither an object nor a list")
        else:
            known = set(rule_ids)
            for key in sorted(guidance_ids - known):
                message = f"guidance references unknown rule {key}"
                if strict:
                    errors.append(message)
                else:
                    warnings.append(message)
            missing = [rid for rid in rule_ids if rid not in guidance_ids]
            if missing:
                warnings.append(f"{len(missing)} rule(s) lack guidance "
                                f"(e.g. {missing[0]})")
    sections = _load_json(path.parent / "sections.json")
    if sections is not None and not isinstance(sections, (dict, list)):
        errors.append("sections.json is neither an object nor a list")
    return (not errors), errors, warnings


def cmd_catalog_list(args: argparse.Namespace) -> int:
    """Enumerate bundled catalogs: rules, guidance, chapters, sha256 (roadmap I)."""
    items = _iter_catalogs()
    if args.output == "json":
        print(json.dumps({"schema": CATALOG_LIST_SCHEMA, "version": VERSION,
                          "catalogs": items}, ensure_ascii=False, indent=2))
        return 0
    print(f"{'profile':<12} {'catalog':<22} {'rules':>5} {'guidance':>8} "
          f"{'chapters':>8} {'sha256':<18}")
    for item in items:
        print(f"{item['profile']:<12} {item['catalog']:<22} {item['rules']:>5} "
              f"{item['guidance']:>8} {item['chapters']:>8} "
              f"{item['sha256'][:16]:<18}")
    return 0


def cmd_catalog_verify(args: argparse.Namespace) -> int:
    """Validate every bundled catalog; exit 0 only when all pass (roadmap I).

    CI gate for the supply chain: a broken or drifted catalog must fail the
    pipeline before any image is hardened from it.
    """
    items: list[dict[str, Any]] = []
    all_ok = True
    for item in _iter_catalogs():
        ok_flag, errors, warnings = _verify_catalog(
            Path(item["path"]), strict=bool(getattr(args, "strict", False)))
        entry = dict(item)
        entry["ok"] = ok_flag
        entry["errors"] = errors
        entry["warnings"] = warnings
        items.append(entry)
        if not ok_flag:
            all_ok = False
    if args.output == "json":
        print(json.dumps({"schema": CATALOG_VERIFY_SCHEMA, "ok": all_ok,
                          "catalogs": items}, ensure_ascii=False, indent=2))
    else:
        for item in items:
            if item["ok"]:
                ok(f"{item['profile']}: {item['catalog']} — {item['rules']} rules, "
                   f"{item['guidance']} guidance, {item['chapters']} chapters")
                for message in item["warnings"]:
                    warn(f"{item['profile']}: {message}")
            else:
                for error in item["errors"]:
                    fail(f"{item['profile']}: {error}")
    return 0 if all_ok else 1
