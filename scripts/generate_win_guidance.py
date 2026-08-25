#!/usr/bin/env python3
"""Regenerate Windows guidance remediation hints from rules.json.

The shipped Windows guidance.json entries previously carried a single
placeholder hint ("Configure via GPO: <title>") — it told the reader nothing
beyond the title. This script derives a real, actionable Group Policy
location for every rule from the rule's section + family, in the project's
own words (no CIS Benchmark text is copied):

  - section 1.x  -> Account Policies (Password / Lockout / Audit)
  - section 2.2  -> Local Policies, User Rights Assignment
  - section 2.3  -> Local Policies, Security Options
  - section 9.x  -> Windows Defender Firewall with Advanced Security
  - section 17.x -> Advanced Audit Policy Configuration
  - section 18.x (Administrative Templates) -> template path derived from
    the top-level component key (18.x maps to System / Network / Windows
    Components), with a stable fallback for leaf sub-sections that cannot
    be mapped deterministically.
  - manual rules are labeled as requiring human evaluation.

The generator is idempotent (run it, commit the diff, run
check_catalog_guidance.py to prove the cross-references still line up).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES_DIR = ROOT / "ohbs_image" / "roles"
WIN_ROLES = ("cis-win2016", "cis-win2019", "cis-win2022", "cis-win2025")

ADM_TEMPLATE_PREFIX = r"Computer Configuration\Administrative Templates"
SECURITY_PREFIX = r"Computer Configuration\Windows Settings\Security Settings"

# Component-key -> Administrative Templates sub-path for section 18.x.
# 18.1-18.9 = System / Network / Windows Components as in the CIS layout.
_ADM_COMPONENT = {
    "18.1": r"System\Group Policy",
    "18.3": r"System\Logon",
    "18.4": r"System\Credentials Delegation",
    "18.5": r"System\Kerberos",
    "18.6": r"Network\Network Connections",
    "18.7": r"Network\Windows Defender Firewall",
    "18.8": r"Network\DNS Client",
    "18.9": r"System\Services",
}
# 18.9.x = System Services (services can be mapped only by name, which the
# title carries, so the leaf hint keeps the setting name explicit).
_ADM_SERVICES = r"System\Services"

_EVENTLOG = r"Windows Components\Event Log Service"
_SAMPLER = r"System\Remote Procedure Call"
# Exact template locations for the few leaf sections we are sure of.
_ADM_LEAF = {
    "18.10.26": rf"{_EVENTLOG}\Application",
    "18.10.26.1": rf"{_EVENTLOG}\Application",
    "18.10.26.2": rf"{_EVENTLOG}\Security",
    "18.10.26.3": rf"{_EVENTLOG}\Setup",
    "18.10.26.4": rf"{_EVENTLOG}\System",
}


def _gpo_hint(rule: dict[str, str]) -> str:
    family = rule.get("family", "")
    section = str(rule.get("section", ""))
    title = rule.get("title", "")

    if family == "manual":
        return ("Manual / organizational policy — requires human evaluation "
                "before deployment; apply the equivalent registry or GPO "
                "setting per your organization's baseline.")

    if section.startswith("1."):
        if family == "password-policy" or family == "password-complexity" \
                or family == "password-reversible":
            sub = "Password Policy"
        elif family == "lockout-policy":
            sub = "Account Lockout Policy"
        else:
            sub = "Account Policies"
        return (f"{SECURITY_PREFIX}\\Account Policies\\{sub}: "
                f"{title.removeprefix('Ensure ')}")

    if section == "2.2":
        return (f"{SECURITY_PREFIX}\\Local Policies\\User Rights "
                f"Assignment: {title.removeprefix('Ensure ')}")

    if section.startswith("2.3."):
        if family == "audit-policy":
            # 2.3.2 "Audit: Force audit policy subcategory settings..." is a
            # Security Options toggle, not an Advanced Audit Policy entry.
            return (f"{SECURITY_PREFIX}\\Local Policies\\Security Options: "
                    f"{title.removeprefix('Ensure ')}")
        return (f"{SECURITY_PREFIX}\\Local Policies\\Security Options: "
                f"{title.removeprefix('Ensure ')}")

    if section.startswith("9."):
        return (f"{SECURITY_PREFIX}\\Windows Defender Firewall with "
                f"Advanced Security: {title.removeprefix('Ensure ')}")

    if family == "eventlog-size":
        return (f"{ADM_TEMPLATE_PREFIX}\\Windows Components\\Event Log "
                f"Service: {title.removeprefix('Ensure ')}")

    if section.startswith("17."):
        return (f"{SECURITY_PREFIX}\\Advanced Audit Policy Configuration\\"
                f"System Audit Policies: {title.removeprefix('Ensure ')}")

    if section.startswith("18."):
        base = section.split(".")[0] + "." + section.split(".")[1] \
            if len(section.split(".")) >= 2 else section
        leaf = _ADM_LEAF.get(section)
        if leaf:
            return f"{ADM_TEMPLATE_PREFIX}\\{leaf}: {title.removeprefix('Ensure ')}"
        comp = _ADM_COMPONENT.get(base)
        if comp:
            return f"{ADM_TEMPLATE_PREFIX}\\{comp}: {title.removeprefix('Ensure ')}"
        if section.startswith("18.9."):
            return (f"{ADM_TEMPLATE_PREFIX}\\{_ADM_SERVICES}: "
                    f"{title.removeprefix('Ensure ')}")
        # Unknown leaf under 18.10+ (Windows Components) — generic but still
        # points to the right tree.
        m = re.match(r"^18\.\d+\.(\d+)", section)
        comp_name = ""
        if m:
            comp_name = f"\\{m.group(1)}"
        return (f"{ADM_TEMPLATE_PREFIX}\\Windows Components{comp_name}: "
                f"{title.removeprefix('Ensure ')}")

    if section.startswith("5."):
        # Service startup policy (e.g. 5.1 Print Spooler) — an
        # Administrative Templates service-startup policy, not a security
        # setting.
        return (f"{ADM_TEMPLATE_PREFIX}\\{_ADM_SERVICES}: "
                f"{title.removeprefix('Ensure ')}")

    return f"{ADM_TEMPLATE_PREFIX}: {title.removeprefix('Ensure ')}"


def main() -> int:
    total = 0
    for role in WIN_ROLES:
        rules_path = ROLES_DIR / role / "files" / "rules.json"
        guidance_path = ROLES_DIR / role / "files" / "guidance.json"
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        guidance = json.loads(guidance_path.read_text(encoding="utf-8"))
        by_id = {str(r["id"]): r for r in rules}
        changed = 0
        for entry in guidance:
            rid = str(entry["id"])
            rule = by_id.get(rid)
            if rule is None:
                continue  # orphan entry — leave untouched (drift check owns it)
            hint = _gpo_hint(rule)
            if entry.get("remediation_hint") != hint:
                entry["remediation_hint"] = hint
                changed += 1
        guidance_path.write_text(
            json.dumps(guidance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"{role}: {len(guidance)} entries, {changed} hints updated")
        total += changed
    print(f"done: {total} hints regenerated across {len(WIN_ROLES)} roles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
