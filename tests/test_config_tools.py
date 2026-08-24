from __future__ import annotations

import argparse
import json

from ohbs_image._config_tools import cmd_config_explain, cmd_config_migrate, cmd_config_schema


def test_schema_is_machine_readable(capsys):
    assert cmd_config_schema(argparse.Namespace(output=None)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["$id"].endswith("/config/v1")
    assert doc["properties"]["schema_version"]["const"] == 1


def test_migrate_preview_does_not_mutate(tmp_path, capsys):
    config = tmp_path / "old.toml"
    config.write_text("[cis]\nlevel = 1\n", encoding="utf-8")
    args = argparse.Namespace(config=str(config), output=None, apply=False)
    assert cmd_config_migrate(args) == 0
    preview = capsys.readouterr().out
    assert "schema_version = 1" in preview and "[ohbs]" in preview
    assert config.read_text(encoding="utf-8").startswith("[cis]")


def test_migrate_apply_is_atomic(tmp_path):
    config = tmp_path / "old.toml"
    config.write_text("[cis]\nlevel = 1\n", encoding="utf-8")
    args = argparse.Namespace(config=str(config), output=None, apply=True)
    assert cmd_config_migrate(args) == 0
    assert config.read_text(encoding="utf-8").startswith("schema_version = 1")
    assert not list(tmp_path.glob("*.tmp"))


def test_explain_known_and_unknown(capsys):
    assert cmd_config_explain(argparse.Namespace(key="state.backend")) == 0
    assert "local or cos" in capsys.readouterr().out
    assert cmd_config_explain(argparse.Namespace(key="unknown.key")) == 1
