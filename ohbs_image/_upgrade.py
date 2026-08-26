from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ._config import _state_dir
from ._logging import VERSION

UPGRADE_SCHEMA = "https://ohbs-image.dev/upgrade-check/v1"
SUPPORTED_STATE_SCHEMA = 2


def _version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError(f"invalid semantic version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def inspect_upgrade(target_version: str, database: Path) -> dict[str, Any]:
    current = _version(VERSION)
    target = _version(target_version)
    issues: list[str] = []
    warnings: list[str] = []
    state_schema: int | None = None
    if target < current:
        warnings.append("target is older than the installed version; use the rollback procedure")
    if target[0] > current[0] + 1:
        issues.append("major-version jumps must be performed one major release at a time")
    if database.exists():
        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            state_schema = int(row[0]) if row else None
        except (sqlite3.Error, TypeError, ValueError) as exc:
            issues.append(f"state database cannot be inspected: {exc}")
        if state_schema is not None and state_schema > SUPPORTED_STATE_SCHEMA:
            issues.append(
                f"state schema {state_schema} is newer than supported schema {SUPPORTED_STATE_SCHEMA}"
            )
    else:
        warnings.append("state database does not exist yet")
    return {
        "schema": UPGRADE_SCHEMA,
        "current_version": VERSION,
        "target_version": target_version.removeprefix("v"),
        "database": str(database.expanduser().resolve()),
        "state_schema": state_schema,
        "supported_state_schema": SUPPORTED_STATE_SCHEMA,
        "compatible": not issues,
        "issues": issues,
        "warnings": warnings,
        "required_steps": [
            "create an online state database backup",
            "verify release provenance and checksums",
            "deploy one canary and verify readiness",
            "retain the previous package or container digest for rollback",
        ],
    }


def cmd_upgrade_check(args: argparse.Namespace) -> int:
    database = Path(args.database) if args.database else _state_dir() / "state.db"
    try:
        result = inspect_upgrade(args.target_version, database)
    except ValueError as exc:
        result = {"schema": UPGRADE_SCHEMA, "compatible": False, "issues": [str(exc)]}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["compatible"] else 1
