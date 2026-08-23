#!/usr/bin/env python3
"""Ensure role-local copies of the CIS execution engines cannot drift.

The role layout is deliberately self-contained for Ansible and Packer.  That
does not make each engine an independent source of truth: current Linux
profiles share one Python engine, as do Windows Server 2016/2019/2025.  This
release gate makes the ownership explicit until the role payloads are
generated from a shared source artifact.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "ohbs_image" / "roles"

GROUPS = {
    "linux": (
        "ohbs_engine.py",
        ("cis-ubuntu2004", "cis-ubuntu2204", "cis-ubuntu2404", "cis-rhel8",
         "cis-rhel9", "cis-rhel10", "cis-tencentos3", "cis-tencentos4"),
    ),
    "windows-legacy": (
        "ohbs_engine.ps1", ("cis-win2016", "cis-win2019", "cis-win2025"),
    ),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    bad = False
    for name, (filename, roles) in GROUPS.items():
        canonical = ROLES / roles[0] / "files" / filename
        if not canonical.is_file():
            print(f"{name}: canonical engine missing: {canonical}", file=sys.stderr)
            bad = True
            continue
        expected = digest(canonical)
        drifted = [role for role in roles
                   if digest(ROLES / role / "files" / filename) != expected]
        if drifted:
            print(f"{name}: engine drift from {roles[0]}: {', '.join(drifted)}", file=sys.stderr)
            bad = True
        else:
            print(f"{name}: {len(roles)} role payloads match {roles[0]}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
