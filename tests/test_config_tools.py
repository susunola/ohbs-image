from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ohbs_image._config import load_config
from ohbs_image._config_tools import (
    cmd_config_diff,
    cmd_config_explain,
    cmd_config_get,
    cmd_config_migrate,
    cmd_config_schema,
    cmd_config_validate,
)
from ohbs_image._logging import ConfigError


def _valid_config(tmp_path: Path) -> Path:
    target = tmp_path / "ohbs-image.toml"
    target.write_text("""schema_version = 1

[build]
profile = "tencentos3"
region = "ap-guangzhou"
zone = "ap-guangzhou-3"
instance_type = "S5.MEDIUM2"
source_image_id = "img-abc12345"
vpc_id = "vpc-abc12345"
subnet_id = "subnet-abc12345"
security_group_id = "sg-abc12345"
associate_public_ip = false

[image]
name_prefix = "tencentos3-cis"
copy_regions = []

[ohbs]
level = 1

[cloud]
secret_id_env = "TENCENTCLOUD_SECRET_ID"
secret_key_env = "TENCENTCLOUD_SECRET_KEY"
""", encoding="utf-8")
    return target


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


# ------------------------------------------------------- roadmap E
class TestConfigValidate:
    def test_valid_config(self, tmp_path, caplog):
        import logging

        from ohbs_image._logging import logger as ohbs_logger
        caplog.set_level(logging.INFO)
        ohbs_logger.setLevel(logging.INFO)
        target = _valid_config(tmp_path)
        assert cmd_config_validate(argparse.Namespace(
            config=str(target), output="text")) == 0
        assert "valid" in caplog.text

    def test_missing_required_key(self, tmp_path, caplog):
        target = _valid_config(tmp_path)
        text = target.read_text(encoding="utf-8").replace(
            'source_image_id = "img-abc12345"', "")
        target.write_text(text, encoding="utf-8")
        assert cmd_config_validate(argparse.Namespace(
            config=str(target), output="text")) == 1
        assert "source_image_id" in caplog.text

    def test_range_violation(self, tmp_path):
        target = _valid_config(tmp_path)
        text = target.read_text(encoding="utf-8").replace(
            "[ohbs]\nlevel = 1", "[ohbs]\nlevel = 1\nmin_score = 150")
        target.write_text(text, encoding="utf-8")
        assert cmd_config_validate(argparse.Namespace(
            config=str(target), output="text")) == 1

    def test_missing_file_is_exit_2(self, tmp_path):
        assert cmd_config_validate(argparse.Namespace(
            config=str(tmp_path / "nope.toml"), output="text")) == 2

    def test_json_output_for_ci(self, tmp_path, capsys):
        target = _valid_config(tmp_path)
        assert cmd_config_validate(argparse.Namespace(
            config=str(target), output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["valid"] is True and doc["errors"] == []


class TestSchemaVersionGuard:
    def test_future_version_rejected(self, tmp_path):
        target = _valid_config(tmp_path)
        text = target.read_text(encoding="utf-8").replace(
            "schema_version = 1", "schema_version = 2")
        target.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match="newer"):
            load_config(target)

    def test_non_integer_version_rejected(self, tmp_path):
        target = _valid_config(tmp_path)
        text = target.read_text(encoding="utf-8").replace(
            "schema_version = 1", 'schema_version = "one"')
        target.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match="integer"):
            load_config(target)

    def test_legacy_config_without_version_is_v1(self, tmp_path):
        target = _valid_config(tmp_path)
        text = target.read_text(encoding="utf-8").replace(
            "schema_version = 1\n\n", "")
        target.write_text(text, encoding="utf-8")
        data = load_config(target)
        assert data["build"]["profile"] == "tencentos3"


class TestConfigDiff:
    def _two(self, tmp_path, right_text):
        left = _valid_config(tmp_path)
        right = tmp_path / "right.toml"
        right.write_text(right_text, encoding="utf-8")
        return left, right

    def test_identical(self, tmp_path, capsys):
        left = _valid_config(tmp_path)
        right = tmp_path / "right.toml"
        right.write_text(left.read_text(encoding="utf-8"), encoding="utf-8")
        assert cmd_config_diff(argparse.Namespace(
            before=str(left), after=str(right), output="text")) == 0

    def test_changed_key_reported(self, tmp_path):
        left = _valid_config(tmp_path)
        right_text = left.read_text(encoding="utf-8").replace(
            'zone = "ap-guangzhou-3"', 'zone = "ap-guangzhou-4"')
        left, right = self._two(tmp_path, right_text)
        assert cmd_config_diff(argparse.Namespace(
            before=str(left), after=str(right), output="text")) == 1

    def test_json_changes_shape(self, tmp_path, capsys):
        left = _valid_config(tmp_path)
        right_text = left.read_text(encoding="utf-8").replace(
            'zone = "ap-guangzhou-3"', 'zone = "ap-guangzhou-4"')
        left, right = self._two(tmp_path, right_text)
        assert cmd_config_diff(argparse.Namespace(
            before=str(left), after=str(right), output="json")) == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc["same"] is False
        assert any(c["key"] == "build.zone" for c in doc["changes"])

    def test_missing_section(self, tmp_path):
        left = _valid_config(tmp_path)
        right_text = left.read_text(encoding="utf-8").replace(
            "[image]\nname_prefix = \"tencentos3-cis\"\ncopy_regions = []\n\n", "")
        left, right = self._two(tmp_path, right_text)
        assert cmd_config_diff(argparse.Namespace(
            before=str(left), after=str(right), output="text")) == 1


class TestConfigGet:
    def test_explicit_value(self, tmp_path, capsys):
        target = _valid_config(tmp_path)
        assert cmd_config_get(argparse.Namespace(
            config=str(target), key="ohbs.level", output="text")) == 0
        assert capsys.readouterr().out.strip() == "1"

    def test_resolved_default_applied(self, tmp_path, capsys):
        target = _valid_config(tmp_path)
        assert cmd_config_get(argparse.Namespace(
            config=str(target), key="build.max_build_minutes", output="text")) == 0
        assert capsys.readouterr().out.strip() == "120"

    def test_raw_default_for_state(self, tmp_path, capsys):
        target = _valid_config(tmp_path)
        assert cmd_config_get(argparse.Namespace(
            config=str(target), key="state.backend", output="text")) == 0
        assert capsys.readouterr().out.strip() == "local"

    def test_json_output(self, tmp_path, capsys):
        target = _valid_config(tmp_path)
        assert cmd_config_get(argparse.Namespace(
            config=str(target), key="ohbs.level", output="json")) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc == {"key": "ohbs.level", "value": 1}

    def test_unknown_key(self, tmp_path):
        target = _valid_config(tmp_path)
        assert cmd_config_get(argparse.Namespace(
            config=str(target), key="nope.nope", output="text")) == 1


class TestExplainAll:
    def test_all_lists_every_section(self, capsys):
        assert cmd_config_explain(argparse.Namespace(key=None, all=True)) == 0
        out = capsys.readouterr().out
        for section in ("[build]", "[image]", "[ohbs]", "[cloud]", "[meta]",
                        "[state]", "[notify]", "[sign]", "[attestation]"):
            assert section in out
        assert "build.max_build_minutes" in out

    def test_no_key_and_no_all_is_error(self):
        assert cmd_config_explain(argparse.Namespace(key=None, all=False)) == 1
