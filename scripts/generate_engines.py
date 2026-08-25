#!/usr/bin/env python3
"""Generate role-local engine payloads from the declared canonical copies.

Roles remain self-contained at package/runtime. Engine ownership is singular:
Linux is authored in cis-ubuntu2004; matching Windows payloads are authored in
cis-win2016. Run with --apply after editing a canonical engine, or without it
to enforce generated-file drift in CI.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "ohbs_image" / "roles"
GROUPS = {
    "linux": ("cis-ubuntu2004", "ohbs_engine.py",
              ("cis-ubuntu2204", "cis-ubuntu2404", "cis-rhel8", "cis-rhel9",
               "cis-rhel10", "cis-rocky9", "cis-tencentos3",
               "cis-tencentos4")),
    "windows-legacy": ("cis-win2016", "ohbs_engine.ps1",
                       ("cis-win2019", "cis-win2022", "cis-win2025")),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    drift = False
    for group, (source_role, filename, targets) in GROUPS.items():
        source = ROLES / source_role / "files" / filename
        payload = source.read_bytes()
        for role in targets:
            target = ROLES / role / "files" / filename
            if target.read_bytes() == payload:
                continue
            drift = True
            if args.apply:
                shutil.copyfile(source, target)
                print(f"generated {target.relative_to(ROOT)} from {source_role}")
            else:
                print(f"{group}: generated payload is stale: {target.relative_to(ROOT)}")
    return 0 if args.apply or not drift else 1


if __name__ == "__main__":
    raise SystemExit(main())
