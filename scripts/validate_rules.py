#!/usr/bin/env python3
"""Semantic validation for the rule catalogs that drive every build.

``ohbs-image`` keeps one ``rules.json`` (and an optional ``guidance.json``)
per OS role under ``ohbs_image/roles/<role>/files/``.  These are the *data* the
vendored engine consumes — 4125 hardening rules across 12 profiles — yet the
only existing guard (``format_rules.py``) checks JSON *layout*, never content.

This script is the missing content gate.  It enforces a real schema so a bad
catalog edit fails CI *before* it can reach an expensive 24-build e2e run:

* required fields present and correctly typed
* enum fields constrained (``levels`` ⊂ {1,2}, ``risk``, ``assessment``,
  ``family`` non-empty, ``platforms`` a list)
* ``id`` unique within a catalog (duplicates would be silently merged)
* ``automated`` field parity: the vendored engine derives it from
  ``assessment``/``risk`` when absent, so Linux catalogs historically shipped
  without it.  We *warn* (not fail) on the known Linux gap and can backfill it.
* ``guidance.json`` (when present) must reference only ``id``s that exist in
  the matching ``rules.json`` (stale guidance hints are a silent data bug).

Pure standard library, safe to run anywhere.  Mirrors ``format_rules.py``'s
exit-code contract: 0 = all clean, 1 = at least one problem.

Usage
-----
    python3 scripts/validate_rules.py                 # check all, concise
    python3 scripts/validate_rules.py --verbose       # list every catalog
    python3 scripts/validate_rules.py --write-backfill  # add missing 'automated'
    python3 scripts/validate_rules.py --strict         # 'automated' gap is an error

Exit code 0 = all catalogs valid; 1 = at least one catalog has a problem
(or fails to parse).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_GLOB = "ohbs_image/roles/*/files/rules.json"
GUIDANCE_GLOB = "ohbs_image/roles/*/files/guidance.json"

# Schema ---------------------------------------------------------------------
REQUIRED_FIELDS = ("id", "title", "section", "levels", "platforms",
                   "assessment", "family", "risk", "page")
STR_FIELDS = ("id", "title", "section", "assessment", "family", "risk")
LIST_FIELDS = ("levels", "platforms")
INT_FIELDS = ("page",)
# Keys that are optional but, when present, must be well-typed.
OPTIONAL_STR = ("note", "defer")
OPTIONAL_BOOL = ("automated", "reboot_required")
OPTIONAL_DICT = ("params",)

VALID_LEVELS = frozenset({1, 2})
VALID_RISK = frozenset({"none", "low", "medium", "high", "safe", "disruptive"})
VALID_ASSESSMENT = frozenset({"Automated", "Manual"})


class Problem:
    """One validation finding: (severity, role, rule_id, message)."""

    def __init__(self, severity: str, role: str, rule_id: str, message: str):
        self.severity = severity  # "error" | "warn"
        self.role = role
        self.rule_id = rule_id
        self.message = message

    def __str__(self) -> str:
        loc = self.role
        if self.rule_id:
            loc = f"{self.role} [{self.rule_id}]"
        tag = "ERROR" if self.severity == "error" else "WARN "
        return f"{tag}  {loc}: {self.message}"


def _role_of(path: Path) -> str:
    # .../roles/<role>/files/rules.json -> <role>
    return path.parts[path.parts.index("roles") + 1]


def _expected_automated(rule: dict) -> bool:
    """The value the engine would derive for ``automated``.

    From the vendored engine (``ohbs_engine.py``): a rule is automated unless
    its assessment is Manual or its risk is 'none' (the "manual review" sentinel).
    """
    return rule.get("assessment") == "Automated" and rule.get("risk") != "none"


def validate_rules(data, role: str, problems: list[Problem]) -> None:
    """Append schema problems found in one catalog's *data* to *problems*."""
    if not isinstance(data, list):
        problems.append(Problem("error", role, "", "rules.json top level must be a JSON array"))
        return

    seen_ids: set[str] = set()
    for idx, rule in enumerate(data):
        rid = rule.get("id", f"#{idx}") if isinstance(rule, dict) else f"#{idx}"
        if not isinstance(rule, dict):
            problems.append(Problem("error", role, rid, f"rule entry is {type(rule).__name__}, expected object"))
            continue

        # Required presence + type.
        for field in REQUIRED_FIELDS:
            if field not in rule:
                problems.append(Problem("error", role, rid, f"missing required field '{field}'"))
        for field in STR_FIELDS:
            if field in rule and not isinstance(rule[field], str):
                problems.append(Problem("error", role, rid, f"'{field}' must be a string"))
        for field in LIST_FIELDS:
            if field in rule and not isinstance(rule[field], list):
                problems.append(Problem("error", role, rid, f"'{field}' must be a list"))
        for field in INT_FIELDS:
            if field in rule and not isinstance(rule[field], int):
                problems.append(Problem("error", role, rid, f"'{field}' must be an integer"))

        # Optional, well-typed when present.
        for field in OPTIONAL_STR:
            if field in rule and not isinstance(rule[field], str):
                problems.append(Problem("error", role, rid, f"'{field}' must be a string"))
        for field in OPTIONAL_BOOL:
            if field in rule and not isinstance(rule[field], bool):
                problems.append(Problem("error", role, rid, f"'{field}' must be a boolean"))
        for field in OPTIONAL_DICT:
            if field in rule and not isinstance(rule[field], dict):
                problems.append(Problem("error", role, rid, f"'{field}' must be an object"))

        # Enum constraints.
        if isinstance(rule.get("levels"), list):
            bad = [lv for lv in rule["levels"] if lv not in VALID_LEVELS]
            if bad:
                problems.append(Problem("error", role, rid, f"'levels' contains invalid value(s): {bad}"))
            if not rule["levels"]:
                problems.append(Problem("error", role, rid, "'levels' must not be empty"))
        if isinstance(rule.get("risk"), str) and rule["risk"] not in VALID_RISK:
            problems.append(Problem("error", role, rid, f"'risk' must be one of {sorted(VALID_RISK)}"))
        if isinstance(rule.get("assessment"), str) and rule["assessment"] not in VALID_ASSESSMENT:
            problems.append(Problem("error", role, rid, f"'assessment' must be one of {sorted(VALID_ASSESSMENT)}"))
        if isinstance(rule.get("family"), str) and not rule["family"]:
            problems.append(Problem("error", role, rid, "'family' must not be empty"))
        if isinstance(rule.get("platforms"), list) and not rule["platforms"]:
            problems.append(Problem("warn", role, rid, "'platforms' is empty — rule applies to no platform"))

        # Uniqueness.
        if isinstance(rule.get("id"), str):
            if rule["id"] in seen_ids:
                problems.append(Problem("error", role, rid, "duplicate id (already defined earlier in this catalog)"))
            seen_ids.add(rule["id"])

        # automated-field parity (historical Linux gap).
        if "automated" not in rule:
            expected = _expected_automated(rule)
            problems.append(Problem(
                "warn", role, rid,
                f"missing 'automated' field (engine would derive {expected}); "
                f"run with --write-backfill to add it",
            ))


def _guidance_ids(guidance) -> set[str]:
    """Normalize either guidance shape to a set of rule ids.

    Linux roles ship a dict keyed by rule id; Windows roles ship a list of
    objects each carrying an ``id``.  Both are accepted.
    """
    if isinstance(guidance, dict):
        return {str(k) for k in guidance}
    if isinstance(guidance, list):
        return {str(e["id"]) for e in guidance if isinstance(e, dict) and "id" in e}
    return set()


def validate_guidance(guidance, rules_path: Path, role: str, problems: list[Problem]) -> None:
    """Ensure every guidance hint references a real rule id in *rules_path*."""
    if guidance is None:
        return  # no guidance.json for this role — that's fine
    if not isinstance(guidance, (dict, list)):
        problems.append(Problem("error", role, "", "guidance.json must be a JSON object or array"))
        return
    try:
        valid_ids = {r.get("id") for r in json.loads(rules_path.read_text(encoding="utf-8"))}
    except Exception:
        return  # rules.json already reported as unparseable; skip cross-check
    for gid in _guidance_ids(guidance):
        if gid not in valid_ids:
            problems.append(Problem("error", role, gid, "guidance references id absent from rules.json"))


def iter_catalogs():
    yield from sorted(REPO_ROOT.glob(RULES_GLOB))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--verbose", action="store_true", help="report every catalog checked")
    p.add_argument("--strict", action="store_true",
                   help="treat the missing-'automated' gap as an error, not a warning")
    p.add_argument("--write-backfill", action="store_true",
                   help="add the derived 'automated' field to rules missing it (rewrites files)")
    args = p.parse_args(argv)

    errors = 0
    warns = 0
    backfilled = 0
    checked = 0

    for rules_path in iter_catalogs():
        role = _role_of(rules_path)
        checked += 1
        problems: list[Problem] = []
        try:
            text = rules_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - report any parse/IO error
            print(f"ERROR  {role}: failed to parse rules.json: {exc}")
            errors += 1
            continue

        validate_rules(data, role, problems)

        # Cross-check guidance.json for the same role, if present.
        guidance_path = rules_path.with_name("guidance.json")
        if guidance_path.exists():
            try:
                gdata = json.loads(guidance_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR  {role}: failed to parse guidance.json: {exc}")
                errors += 1
                gdata = None
            validate_guidance(gdata, rules_path, role, problems)

        # Optional backfill of the 'automated' field.
        if args.write_backfill:
            changed = False
            for rule in data if isinstance(data, list) else []:
                if isinstance(rule, dict) and "automated" not in rule:
                    rule["automated"] = _expected_automated(rule)
                    changed = True
            if changed:
                rules_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                backfilled += 1
                if args.verbose:
                    print(f"backfilled  {role}: added 'automated' to missing rules")

        for prob in problems:
            if prob.severity == "error" or (prob.severity == "warn" and args.strict):
                errors += 1
                print(prob)
            else:
                warns += 1
                if args.verbose:
                    print(prob)

    # Summary.
    if errors:
        print(f"\n{errors} error(s), {warns} warning(s) across {checked} catalog(s).")
        if args.write_backfill:
            print(f"Backfilled 'automated' into {backfilled} catalog(s) "
                  f"— re-run without --write-backfill to confirm clean.")
        return 1

    msg = f"all {checked} rule catalog(s) valid"
    if warns:
        msg += f" ({warns} warning(s) — non-blocking)"
    if backfilled:
        msg += f"; backfilled {backfilled} catalog(s)"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
