from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ._logging import VERSION, fail, ok
from ._profiles import PROFILES

ENGINE_LIST_SCHEMA = "https://ohbs-image.dev/engine-list/v1"
ENGINE_VERIFY_SCHEMA = "https://ohbs-image.dev/engine-verify/v1"

# Engine scripts ship per-role under roles/<role_dir>/files/.
_LINUX_ENGINE = "ohbs_engine.py"
_WIN_ENGINE = "ohbs_engine.ps1"


def _engine_file_name(role_dir: str) -> str:
    return _WIN_ENGINE if role_dir.startswith("cis-win") else _LINUX_ENGINE


def _engine_path(role_dir: str) -> Path:
    """Return the bundled engine script path for a role directory."""
    return (Path(__file__).parent / "roles" / role_dir
            / "files" / _engine_file_name(role_dir))


def _engine_version(path: Path) -> str:
    """Parse the version constant embedded in an engine script ("-" if absent)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "-"
    if path.name.endswith(".ps1"):
        match = re.search(r"engine_version\s*=\s*[\"']([^\"']+)[\"']", content)
    else:
        match = re.search(r"^VERSION\s*=\s*[\"']([^\"']+)[\"']", content,
                          re.MULTILINE)
    return match.group(1) if match else "-"


def _engine_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "-"


def _engine_syntax_ok(path: Path) -> list[str]:
    """Return syntax/well-formedness errors (empty list = engine is fine).

    Linux engines are parsed as Python (no bytecode is written); Windows
    engines are checked for emptiness and NUL bytes — a cheap gate that
    catches the common corruption modes without running PowerShell.
    """
    if not path.is_file():
        return [f"engine file missing: {path}"]
    try:
        content = path.read_bytes()
    except OSError as exc:
        return [f"engine unreadable: {exc}"]
    if path.name.endswith(".ps1"):
        if not content.strip():
            return ["PowerShell engine is empty"]
        if b"\x00" in content:
            return ["PowerShell engine contains NUL bytes"]
        return []
    if not content.strip():
        return ["Python engine is empty"]
    try:
        ast.parse(content.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"Python engine syntax error: {exc}"]
    return []


def _iter_engines() -> list[dict[str, Any]]:
    """Per-profile engine metadata for every bundled profile."""
    items: list[dict[str, Any]] = []
    for name, meta in sorted(PROFILES.items()):
        role = str(meta.get("role_dir", ""))
        path = _engine_path(role)
        family = "windows" if meta.get("family") == "windows" else "linux"
        items.append({
            "profile": name,
            "family": family,
            "engine": path.name,
            "path": str(path),
            "version": _engine_version(path),
            "sha256": _engine_sha256(path),
            "bytes": path.stat().st_size if path.is_file() else 0,
        })
    return items


def cmd_engine_list(args: argparse.Namespace) -> int:
    """Enumerate the bundled engines with version + sha256 (roadmap H)."""
    items = _iter_engines()
    if args.output == "json":
        print(json.dumps({"schema": ENGINE_LIST_SCHEMA, "version": VERSION,
                          "engines": items}, ensure_ascii=False, indent=2))
        return 0
    print(f"{'profile':<12} {'family':<8} {'version':<16} {'sha256':<18} "
          f"{'bytes':>5}  engine")
    for item in items:
        print(f"{item['profile']:<12} {item['family']:<8} "
              f"{item['version']:<16} {item['sha256'][:16]:<18} "
              f"{item['bytes']:>5}  {item['engine']}")
    return 0


def cmd_engine_verify(args: argparse.Namespace) -> int:
    """Syntax-check every bundled engine; exit 0 only when all pass (roadmap H)."""
    items: list[dict[str, Any]] = []
    all_ok = True
    for item in _iter_engines():
        errors = _engine_syntax_ok(Path(item["path"]))
        entry = dict(item)
        entry["ok"] = not errors
        entry["errors"] = errors
        items.append(entry)
        if errors:
            all_ok = False
    if args.output == "json":
        print(json.dumps({"schema": ENGINE_VERIFY_SCHEMA, "ok": all_ok,
                          "engines": items}, ensure_ascii=False, indent=2))
    else:
        for item in items:
            if item["ok"]:
                ok(f"{item['profile']}: engine {item['version']} "
                   f"({item['engine']}, {item['bytes']} bytes)")
            else:
                for error in item["errors"]:
                    fail(f"{item['profile']}: {error}")
    return 0 if all_ok else 1


def cmd_engine_version(args: argparse.Namespace) -> int:
    """Print ohbs-image plus per-family engine versions (roadmap H)."""
    print(f"ohbs-image {VERSION}")
    for family in ("linux", "windows"):
        versions = sorted({item["version"] for item in _iter_engines()
                           if item["family"] == family and item["version"] != "-"})
        if versions:
            print(f"engine ({family}): {', '.join(versions)}")
    return 0
