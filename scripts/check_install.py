#!/usr/bin/env python3
"""check_install.py — detect a stale non-editable install.

Compares the CIS role files (engines + catalogs) in this source checkout
with the ones actually installed in site-packages.  A plain
`pip install .` freezes the code: after a `git pull` the installed CLI
and bundled roles silently go stale (observed: a full build round ran
the previous engine).  Exit 0 when in sync, 1 on drift.

Usage:  python3 scripts/check_install.py [--quiet]
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _hash_tree(base: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main() -> int:
    quiet = "--quiet" in sys.argv
    spec = importlib.util.find_spec("ohbs_image")
    if spec is None or not spec.origin:
        print("check_install: ohbs_image is not importable — not installed?", file=sys.stderr)
        return 1
    installed = Path(spec.origin).parent / "roles"
    local = ROOT / "ohbs_image" / "roles"
    if not installed.is_dir():
        print(f"check_install: installed roles dir missing at {installed}", file=sys.stderr)
        return 1

    # Editable installs point at the same tree — always in sync.
    if installed.resolve() == local.resolve():
        if not quiet:
            print("check_install: editable install, in sync")
        return 0

    li, ii = _hash_tree(local), _hash_tree(installed)
    drift = [k for k in li if li[k] != ii.get(k)] + [k for k in ii if k not in li]
    if drift:
        print(f"check_install: STALE install — {len(drift)} role file(s) differ "
              f"between checkout and {installed}:", file=sys.stderr)
        for k in drift[:10]:
            print(f"  {k}", file=sys.stderr)
        print("check_install: run `pip install --force-reinstall .`", file=sys.stderr)
        return 1
    if not quiet:
        print("check_install: in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
