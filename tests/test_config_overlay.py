"""Roadmap E — config overlay / layered configuration tests.

Covers deep_merge + load_config_layered semantics (later layers win;
tables recurse; lists/scalars replace), the `config merge` CLI contract
(exit 0/1/2), and the repeatable `--overlay` flag honored by commands
(plan / doctor / preflight / build etc. via the common parser).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from ohbs_image._cli import main
from ohbs_image._commands import _load_resolved
from ohbs_image._config import deep_merge, load_config, load_config_layered
from ohbs_image._config_tools import cmd_config_merge
from ohbs_image._logging import ConfigError

BASE_TOML = """schema_version = 1

[build]
profile = "tencentos3"
region = "ap-guangzhou"
zone = "ap-guangzhou-3"
instance_type = "S5.MEDIUM2"
source_image_id = "img-abc12345"
vpc_id = "vpc-abc12345"
subnet_id = "subnet-abc12345"
security_group_id = "sg-abc12345"
associate_public_ip = true
max_build_minutes = 60

[image]
name_prefix = "cis"
copy_regions = ["ap-shanghai", "ap-beijing"]

[ohbs]
level = 1

[cloud]
secret_id_env = "TENCENTCLOUD_SECRET_ID"
secret_key_env = "TENCENTCLOUD_SECRET_KEY"
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# deep_merge semantics
# ---------------------------------------------------------------------------
class TestDeepMerge:
    def test_scalar_override(self):
        merged = deep_merge({"a": 1, "b": 2}, {"b": 3})
        assert merged == {"a": 1, "b": 3}

    def test_tables_merge_recursively(self):
        merged = deep_merge(
            {"build": {"zone": "z3", "region": "gz", "max": 60}},
            {"build": {"zone": "z6", "max": 90}},
        )
        assert merged["build"] == {"zone": "z6", "region": "gz", "max": 90}

    def test_list_is_replaced_not_appended(self):
        merged = deep_merge({"image": {"copy_regions": ["a", "b"]}},
                            {"image": {"copy_regions": ["c"]}})
        assert merged["image"]["copy_regions"] == ["c"]

    def test_nested_tables(self):
        merged = deep_merge(
            {"ohbs": {"overrides": {"1.1.1": {"mtype": "fs"}}}},
            {"ohbs": {"overrides": {"1.1.1": {"module": "cramfs"}}}},
        )
        assert merged["ohbs"]["overrides"]["1.1.1"] == {"module": "cramfs", "mtype": "fs"}

    def test_inputs_are_not_mutated(self):
        base = {"a": {"x": 1}}
        overlay = {"a": {"y": 2}, "b": 1}
        deep_merge(base, overlay)
        assert base == {"a": {"x": 1}}
        assert overlay == {"a": {"y": 2}, "b": 1}


# ---------------------------------------------------------------------------
# load_config_layered
# ---------------------------------------------------------------------------
class TestLoadConfigLayered:
    def test_later_file_wins(self, tmp_path):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        overlay = _write(tmp_path, "overlay.toml",
                         "[build]\nzone = \"ap-guangzhou-6\"\nmax_build_minutes = 90\n"
                         "[image]\ncopy_regions = [\"ap-singapore\"]\n[ohbs]\nlevel = 2\n")
        merged = load_config_layered([base, overlay])
        assert merged["build"]["zone"] == "ap-guangzhou-6"
        assert merged["build"]["max_build_minutes"] == 90
        assert merged["image"]["copy_regions"] == ["ap-singapore"]
        assert merged["ohbs"]["level"] == 2
        # fields only in base are preserved
        assert merged["build"]["profile"] == "tencentos3"
        assert merged["build"]["region"] == "ap-guangzhou"

    def test_single_file_matches_load_config(self, tmp_path):
        path = _write(tmp_path, "base.toml", BASE_TOML)
        assert load_config_layered([path]) == load_config(path)

    def test_three_layers(self, tmp_path):
        p1 = _write(tmp_path, "p1.toml", BASE_TOML)
        p2 = _write(tmp_path, "p2.toml", "[ohbs]\nlevel = 2\n[build]\nzone = \"z6\"\n")
        p3 = _write(tmp_path, "p3.toml", "[build]\nzone = \"z7\"\n")
        merged = load_config_layered([p1, p2, p3])
        assert merged["ohbs"]["level"] == 2
        assert merged["build"]["zone"] == "z7"

    def test_missing_file_raises(self, tmp_path):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        with pytest.raises(ConfigError, match="not found"):
            load_config_layered([base, tmp_path / "nope.toml"])

    def test_empty_paths_raises(self):
        with pytest.raises(ConfigError, match="No configuration files"):
            load_config_layered([])

    def test_invalid_merged_result_fails_validation(self, tmp_path):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        bad = _write(tmp_path, "bad.toml", "[ohbs]\nlevel = 3\n")
        with pytest.raises(ConfigError, match="level must be 1 or 2"):
            load_config_layered([base, bad])

    def test_schema_version_gate_applies_to_merged(self, tmp_path):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        future = _write(tmp_path, "future.toml", "schema_version = 9\n[ohbs]\nlevel = 2\n")
        with pytest.raises(ConfigError, match="newer than"):
            load_config_layered([base, future])


# ---------------------------------------------------------------------------
# config merge CLI
# ---------------------------------------------------------------------------
class TestCmdConfigMerge:
    def _args(self, tmp_path: Path, *overlays: str, output: str | None = None,
              output_json: bool = False) -> argparse.Namespace:
        base = _write(tmp_path, "base.toml", BASE_TOML)
        paths = [str(base)] + list(overlays)
        return argparse.Namespace(base=str(base), overlays=list(overlays),
                                  output=output, output_json=output_json)

    def test_valid_merge_exit_0(self, tmp_path, capsys):
        overlay = _write(tmp_path, "o.toml", "[ohbs]\nlevel = 2\n[build]\nzone = \"z6\"\n")
        rc = cmd_config_merge(self._args(tmp_path, str(overlay)))
        out = capsys.readouterr().out
        assert rc == 0
        assert "[ohbs]" in out and "level = 2" in out
        assert "zone = \"z6\"" in out
        # the synthetic [cis] alias must never leak back into user output
        assert "[cis]" not in out

    def test_missing_file_exit_2(self, tmp_path, caplog):
        rc = cmd_config_merge(self._args(tmp_path, str(tmp_path / "nope.toml")))
        assert rc == 2
        # cmd_config_merge reports errors through the logging module (no
        # StreamHandler when called directly), so assert on caplog.
        assert "not found" in caplog.text

    def test_invalid_merged_exit_1(self, tmp_path, caplog):
        bad = _write(tmp_path, "bad.toml", "[ohbs]\nlevel = 3\n")
        rc = cmd_config_merge(self._args(tmp_path, str(bad)))
        assert rc == 1
        assert "level must be 1 or 2" in caplog.text

    def test_output_json(self, tmp_path, capsys):
        overlay = _write(tmp_path, "o.toml", "[ohbs]\nlevel = 2\n")
        rc = cmd_config_merge(self._args(tmp_path, str(overlay), output_json=True))
        doc = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert doc["valid"] is True
        assert len(doc["layers"]) == 2

    def test_output_writes_file(self, tmp_path):
        overlay = _write(tmp_path, "o.toml", "[ohbs]\nlevel = 2\n")
        out_path = tmp_path / "merged.toml"
        rc = cmd_config_merge(self._args(tmp_path, str(overlay), output=str(out_path)))
        assert rc == 0
        text = out_path.read_text(encoding="utf-8")
        assert "level = 2" in text and "[cis]" not in text


# ---------------------------------------------------------------------------
# --overlay flag end-to-end
# ---------------------------------------------------------------------------
class TestOverlayFlag:
    def test_load_resolved_honors_overlays(self, tmp_path):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        overlay = _write(tmp_path, "o.toml", "[ohbs]\nlevel = 2\n[build]\nzone = \"z6\"\n")
        r = _load_resolved(str(base), [str(overlay)])
        assert r is not None
        assert r.level == 2
        assert r.zone == "z6"

    def test_load_resolved_without_overlay_unchanged(self, tmp_path):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        r = _load_resolved(str(base))
        assert r is not None
        assert r.level == 1
        assert r.zone == "ap-guangzhou-3"

    def test_plan_dry_run_shows_overlay_values(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        base = _write(tmp_path, "base.toml", BASE_TOML)
        overlay = _write(tmp_path, "o.toml",
                         "[ohbs]\nlevel = 2\n[build]\nzone = \"ap-guangzhou-6\"\n"
                         "max_build_minutes = 90\n")
        rc = main(["plan", "--dry-run",
                   "--config", str(base), "--overlay", str(overlay),
                   "--workdir", str(tmp_path / "wd")])
        assert rc == 0
        # plan's human-readable dry-run output goes through logging (info),
        # not stdout; assert on caplog to stay capture-state independent.
        assert "CIS L2 in ap-guangzhou-6" in caplog.text
        assert "90 minutes" in caplog.text

    def test_plan_without_overlay_unchanged(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        base = _write(tmp_path, "base.toml", BASE_TOML)
        rc = main(["plan", "--dry-run", "--config", str(base),
                   "--workdir", str(tmp_path / "wd")])
        assert rc == 0
        assert "CIS L1 in ap-guangzhou-3" in caplog.text

    def test_missing_overlay_fails_command(self, tmp_path, caplog):
        base = _write(tmp_path, "base.toml", BASE_TOML)
        rc = main(["plan", "--dry-run",
                   "--config", str(base), "--overlay", str(tmp_path / "nope.toml"),
                   "--workdir", str(tmp_path / "wd")])
        assert rc == 2
        # Same reasoning as the plan tests: the failure is reported through
        # logging; assert on caplog to stay capture-mode independent.
        assert "not found" in caplog.text
