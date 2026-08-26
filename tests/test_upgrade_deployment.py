from __future__ import annotations

import sqlite3
from pathlib import Path

from ohbs_image._upgrade import SUPPORTED_STATE_SCHEMA, inspect_upgrade


def test_upgrade_check_accepts_supported_database(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES('schema_version', ?)",
                           (str(SUPPORTED_STATE_SCHEMA),))
    result = inspect_upgrade("0.20.0", database)
    assert result["compatible"] is True
    assert result["state_schema"] == SUPPORTED_STATE_SCHEMA


def test_upgrade_check_rejects_future_schema(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES('schema_version', '999')")
    assert inspect_upgrade("0.20.0", database)["compatible"] is False


def test_deployment_assets_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in ("deploy/docker-compose.yml", "deploy/kubernetes.yaml",
                     "deploy/ohbs-image.service", "docs/production-deployment.md"):
        assert (root / relative).is_file()
