#!/usr/bin/env python3
"""Fail CI when Markdown links or documented top-level CLI commands drift."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
COMMAND_RE = re.compile(r"^ohbs-image\s+([a-z][a-z0-9-]*)")
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def markdown_files(root: Path) -> list[Path]:
    files = [root / "README.md"]
    files.extend(sorted((root / "docs").glob("*.md")))
    return [path for path in files if path.is_file()]


def documented_commands(text: str) -> set[str]:
    found: set[str] = set()
    samples = INLINE_CODE_RE.findall(text)
    for block in FENCE_RE.findall(text):
        samples.extend(block.splitlines())
    for sample in samples:
        normalized = sample.strip()
        if normalized.startswith("$ "):
            normalized = normalized[2:].lstrip()
        match = COMMAND_RE.match(normalized)
        if match:
            found.add(match.group(1))
    return found


def check_documents(root: Path, commands: set[str]) -> list[str]:
    errors: list[str] = []
    for path in markdown_files(root):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root)
        for command in sorted(documented_commands(text) - commands):
            errors.append(f"{relative}: unknown documented command 'ohbs-image {command}'")
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            local = unquote(target.split("#", 1)[0])
            if not local:
                continue
            resolved = (path.parent / local).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{relative}: local link escapes repository: {target}")
                continue
            if not resolved.exists():
                errors.append(f"{relative}: broken local link: {target}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.root))
    from _cli_introspection import registered_subcommands

    from ohbs_image import build_parser

    commands = registered_subcommands(build_parser())
    errors = check_documents(args.root, commands)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"documentation verified: {len(markdown_files(args.root))} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
