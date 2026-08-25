#!/usr/bin/env python3
"""Verify every bundled CIS catalog can safely enrich a delivery report."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "ohbs_image" / "roles"


def rule_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return (10**9,)


def check_role(role: Path) -> list[str]:
    files = role / "files"
    catalog_path = files / "rules.json"
    guidance_path = files / "guidance.json"
    errors: list[str] = []
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{catalog_path}: invalid JSON: {exc}"]
    if not isinstance(catalog, list):
        return [f"{catalog_path}: expected an array"]
    ids = [str(rule.get("id", "")) for rule in catalog if isinstance(rule, dict)]
    if len(ids) != len(catalog) or any(not rule_id for rule_id in ids):
        errors.append(f"{catalog_path}: every rule needs an id")
    if len(set(ids)) != len(ids):
        errors.append(f"{catalog_path}: duplicate rule IDs")
    if ids != sorted(ids, key=rule_key):
        errors.append(f"{catalog_path}: rule IDs are not in numeric CIS order")
    try:
        guidance = json.loads(guidance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"{guidance_path}: invalid JSON: {exc}"]
    if isinstance(guidance, dict):
        entries = guidance
    elif isinstance(guidance, list):
        entries = {str(entry["id"]): entry for entry in guidance
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        if len(entries) != len(guidance):
            errors.append(f"{guidance_path}: every list entry needs a string id")
    else:
        return errors + [f"{guidance_path}: expected an object or array"]
    # Older Windows guidance exports can retain benchmark-changelog entries
    # for removed controls. They are ignored by report hydration; do not let
    # them hide validation of the active catalog's entries.
    unknown = sorted(set(entries) - set(ids), key=rule_key)
    if unknown:
        print(f"warning: {guidance_path}: stale guidance ignored: {', '.join(unknown[:8])}",
              file=sys.stderr)
    malformed = [rule_id for rule_id, entry in entries.items() if not isinstance(entry, dict)]
    if malformed:
        errors.append(f"{guidance_path}: non-object guidance entries: {', '.join(malformed[:8])}")
    return errors


def main() -> int:
    errors: list[str] = []
    roles = sorted(path for path in ROLES.iterdir() if path.is_dir())
    for role in roles:
        errors.extend(check_role(role))
    if errors:
        print("catalog guidance check failed:", *errors, sep="\n", file=sys.stderr)
        return 1
    print(f"catalog guidance: {len(roles)} role catalogs and guidance maps OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
