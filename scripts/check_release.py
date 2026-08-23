#!/usr/bin/env python3
"""Fail when a release tag, package version, and changelog disagree."""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def package_version() -> str:
    tree = ast.parse((ROOT / "ohbs_image" / "_logging.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets
        ) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise ValueError("VERSION not found in ohbs_image/_logging.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v0.18.0")
    args = parser.parse_args(argv)
    version = package_version()
    expected = f"v{version}"
    if args.tag != expected:
        print(f"release tag {args.tag!r} does not match package VERSION {version!r}", file=sys.stderr)
        return 1
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(version)}\](?:\s|$)", changelog, re.MULTILINE):
        print(f"CHANGELOG.md has no release heading for {version}", file=sys.stderr)
        return 1
    print(f"release metadata OK: {args.tag} / {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
