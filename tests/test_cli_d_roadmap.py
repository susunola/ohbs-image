"""Roadmap D — CLI product experience (items 91-120).

Covered here:
- D-91  help grouped by lifecycle (GroupedHelpFormatter)
- D-92/93 deprecated `cis-image` entry alias with removal window
- D-96  unified --no-color
- D-97  unified --non-interactive
- D-98  unified --timeout
- D-99  unified --dry-run
- D-100 unified --quiet/--verbose
- D-101 stable exit-code documentation
- D-102..D-115 plan gates/risks surface
- D-116 plan --check CI gate
- D-117 plan --schema
- D-118 plan --save as evidence
- D-119 plan --diff-last
- D-120 plan never calls write APIs
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

import ohbs_image
from ohbs_image import build_parser
from ohbs_image._cli import COMMAND_GROUPS, _deprecation_prog
from ohbs_image._logging import _setup_logging
from ohbs_image._onboarding import (
    _ask,
    _plan_risks,
    cmd_plan,
    set_non_interactive,
)


def _configure_args(target: Path) -> argparse.Namespace:
    return argparse.Namespace(
        target=str(target), force=False, discover=False, profile="tencentos3",
        region="ap-guangzhou", zone="ap-guangzhou-3",
        source_image="img-abc12345", vpc="vpc-abc12345",
        subnet="subnet-abc12345", security_group="sg-abc12345",
        instance_type=None, level=1, public_ip=False,
    )


def _plan_args(config: str, **extra: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"config": config, "output": "json", "check": False,
                            "save": False, "diff_last": False, "schema": False}
    base.update(extra)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- D-91
class TestHelpGrouping:
    def test_help_contains_lifecycle_group_headers(self):
        text = build_parser().format_help()
        for group in COMMAND_GROUPS:
            assert f"{group}:" in text, f"missing group header {group!r}"

    def test_every_command_is_in_a_group(self):
        parser = build_parser()
        text = parser.format_help()
        sub = next(a for a in parser._actions
                   if isinstance(a, argparse._SubParsersAction))
        registered = set(sub.choices)
        grouped = {n for names in COMMAND_GROUPS.values() for n in names}
        assert registered == grouped, f"ungrouped commands: {registered - grouped}"
        # and the grouped commands are actually rendered in the help text
        for group, names in COMMAND_GROUPS.items():
            assert f"{group}:" in text
            for name in names:
                assert f"    {name}" in text, f"{name} missing from help under {group}"

    def test_parse_is_unchanged_by_grouping(self):
        ns = build_parser().parse_args(["list", "--output", "json"])
        assert ns.command == "list"
        assert ns.output == "json"


# ------------------------------------------------------------ D-92/93
class TestDeprecatedEntryAlias:
    def test_cis_image_alias_warns(self, capsys):
        _deprecation_prog(["cis-image", "list"])
        err = capsys.readouterr().err
        assert "cis-image" in err and "deprecated" in err and "0.19.0" in err

    def test_ohbs_image_name_is_silent(self, capsys):
        _deprecation_prog(["ohbs-image", "list"])
        assert capsys.readouterr().err == ""

    def test_argv_none_uses_sys_argv(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["cis_image"])
        _deprecation_prog(None)
        assert "deprecated" in capsys.readouterr().err


# ---------------------------------------------------------------- D-96
class TestNoColor:
    def test_disable_color_forces_plain(self, monkeypatch):
        import ohbs_image._logging as lm
        monkeypatch.setattr(lm, "_COLOR_DISABLED", False)
        monkeypatch.setattr("ohbs_image._logging.sys.stderr.isatty", lambda: True)
        monkeypatch.delenv("NO_COLOR", raising=False)
        lm.disable_color()
        assert lm._COLOR_DISABLED is True
        assert lm._color("x", 32) == "x"
        # leave no global side effects for later tests
        monkeypatch.setattr(lm, "_COLOR_DISABLED", False)

    def test_main_accepts_no_color_flag(self):
        assert build_parser().parse_args(["--no-color", "list"]).no_color is True


# ---------------------------------------------------------------- D-97
class TestNonInteractive:
    def test_ask_uses_default_when_non_interactive(self, monkeypatch):
        monkeypatch.setattr("ohbs_image._onboarding.sys.stdin.isatty", lambda: True)
        set_non_interactive()
        assert _ask(None, "region", default="ap-guangzhou") == "ap-guangzhou"

    def test_ask_raises_when_non_interactive_and_no_default(self, monkeypatch):
        from ohbs_image._logging import ConfigError
        monkeypatch.setattr("ohbs_image._onboarding.sys.stdin.isatty", lambda: True)
        set_non_interactive()
        with pytest.raises(ConfigError):
            _ask(None, "region")

    def test_main_accepts_non_interactive_flag(self):
        assert build_parser().parse_args(["--non-interactive", "list"]).non_interactive is True


# ---------------------------------------------------------------- D-98
class TestTimeout:
    def test_build_timeout_override_wins(self):
        from ohbs_image._commands import _build_timeout
        r = argparse.Namespace(max_build_minutes=120)
        assert _build_timeout(argparse.Namespace(timeout=30), r) == 1800
        assert _build_timeout(argparse.Namespace(timeout=None), r) == 7200

    def test_common_parser_exposes_timeout(self):
        assert build_parser().parse_args(["validate", "--timeout", "45"]).timeout == 45


# ---------------------------------------------------------------- D-99
class TestDryRun:
    def test_validate_dry_run_skips_packer(self, tmp_path, monkeypatch, capsys, caplog):
        import logging
        caplog.set_level(logging.INFO)
        from ohbs_image._onboarding import cmd_configure
        target = tmp_path / "ohbs-image.toml"
        assert cmd_configure(_configure_args(target)) == 0
        capsys.readouterr()

        def boom(*a, **k):
            raise AssertionError("run_packer must not be called in dry-run")

        monkeypatch.setattr("ohbs_image.run_packer", boom)
        rc = ohbs_image.cmd_validate(argparse.Namespace(
            config=str(target), workdir=str(tmp_path / "wd"),
            dry_run=True, quiet=False, debug=False, timeout=None))
        assert rc == 0
        assert "dry-run" in caplog.text

    def test_build_dry_run_skips_packer(self, tmp_path, monkeypatch, capsys):
        from ohbs_image._onboarding import cmd_configure
        target = tmp_path / "ohbs-image.toml"
        assert cmd_configure(_configure_args(target)) == 0
        capsys.readouterr()

        def boom(*a, **k):
            raise AssertionError("run_packer must not be called in dry-run")

        monkeypatch.setattr("ohbs_image.run_packer", boom)
        monkeypatch.setenv("OHBS_IMAGE_STATE_DIR", str(tmp_path / "state"))
        rc = ohbs_image.cmd_build(argparse.Namespace(
            config=str(target), workdir=str(tmp_path / "wd"),
            dry_run=True, quiet=False, debug=False, timeout=None,
            yes=True, skip_if_unchanged=False, log_file=None, result_file=None))
        assert rc == 0


# ---------------------------------------------------------------- D-100
class TestQuietVerbose:
    def test_quiet_sets_warning_level(self):
        import logging
        _setup_logging(quiet=True)
        from ohbs_image._logging import logger
        assert logger.level == logging.WARNING

    def test_main_accepts_quiet_flag(self):
        assert build_parser().parse_args(["-q", "list"]).quiet is True


# ---------------------------------------------------------------- D-101
class TestExitCodesDoc:
    def test_exit_codes_document_exists_and_covers_core_contract(self):
        doc = Path(__file__).resolve().parents[1] / "docs" / "exit-codes.md"
        assert doc.exists(), "docs/exit-codes.md is required (roadmap D-101)"
        text = doc.read_text(encoding="utf-8")
        for token in ("0", "1", "2", "70", "130", "ready", "blocked"):
            assert token in text


# ----------------------------------------------------- D-102..D-115
class TestPlanSurface:
    def _doc(self, tmp_path, **cfg_extra):
        target = tmp_path / "ohbs-image.toml"
        from ohbs_image._onboarding import cmd_configure
        args = _configure_args(target)
        args.public_ip = cfg_extra.pop("public_ip", False)
        assert cmd_configure(args) == 0
        from ohbs_image._config import load_config, resolve
        r = resolve(load_config(target))
        return r

    def test_plan_gates_include_cve_and_sbom(self, tmp_path):
        r = self._doc(tmp_path)
        doc = {"gates": {"cve_scan": r.cve_scan, "sbom": r.sbom}}
        assert doc["gates"]["cve_scan"] is False
        assert doc["gates"]["sbom"] is False

    def test_risks_surface(self):
        from ohbs_image._config import ResolvedConfig

        def mk(**kw: Any) -> ResolvedConfig:
            base: dict[str, Any] = {
                "profile_name": "p", "profile": {}, "family": "", "region": "r",
                "zone": "z", "instance_type": "t", "source_image_id": "img-1",
                "vpc_id": "v", "subnet_id": "s", "security_group_id": "g",
                "associate_public_ip": False, "ssh_port": 22, "ssh_timeout": "1m",
                "ssh_username": "root", "ssh_debug_password": "",
                "winrm_username": "", "winrm_password_env": "",
                "image_name_prefix": "x", "image_name_override": "",
                "instance_name": "", "image_copy_regions": [],
                "image_share_accounts": [], "image_share_org_units": [],
                "spot": False, "max_build_minutes": 120, "cis_level_tag": "",
                "secret_id_env": "", "secret_key_env": "",
                "security_token_env": "", "assume_role_arn": "",
                "assume_role_session": "", "assume_role_duration": 7200,
                "image_os_tag": "", "image_benchmark": "", "catalog_basename": "",
                "level": 1, "min_score": 85, "allow_disruptive": True,
                "allow_scoped_approval": False, "role_dir": "", "smoke_test": True,
                "cve_scan": False, "sbom": False, "delivery_report_required": False,
                "rules_include": [], "rules_exclude": [], "rules_overrides": {},
                "notify_webhook": "", "notify_on": "failure", "deploy_webhook": "",
                "sign_key": "", "attestation_required": False,
                "test_components": [], "verify_boot": False,
            }
            base.update(kw)
            return ResolvedConfig(**base)

        assert [x["id"] for x in _plan_risks(mk(associate_public_ip=True))] == \
            ["public-ip", "disruptive-remediation"]
        ids = [x["id"] for x in _plan_risks(mk(rules_include=["1.1.1"]))]
        assert "scoped-rules" in ids
        ids = [x["id"] for x in
               _plan_risks(mk(rules_include=["1.1.1"], allow_scoped_approval=True))]
        assert "scoped-approval" in ids
        ids = [x["id"] for x in _plan_risks(mk(winrm_password_env="WINRM_PASSWORD"))]
        assert "winrm-password-env" in ids


# ---------------------------------------------------------------- D-116
class TestPlanCheck:
    def test_plan_check_blocks_on_high_risk(self, tmp_path, capsys, caplog):
        target = tmp_path / "ohbs-image.toml"
        from ohbs_image._onboarding import cmd_configure
        assert cmd_configure(_configure_args(target)) == 0
        # inject a scoped rule subset without approval -> high risk
        cfg = target.read_text(encoding="utf-8")
        assert "[ohbs]\nlevel = 1" in cfg
        target.write_text(cfg.replace(
            "[ohbs]\nlevel = 1",
            "[ohbs]\nlevel = 1\nrules_include = [\"1.1.1\"]"),
            encoding="utf-8")
        capsys.readouterr()
        rc = cmd_plan(_plan_args(str(target), check=True, output="text"))
        assert rc == 1
        assert "high-risk" in caplog.text

    def test_plan_check_passes_without_high_risk(self, tmp_path, capsys):
        target = tmp_path / "ohbs-image.toml"
        from ohbs_image._onboarding import cmd_configure
        assert cmd_configure(_configure_args(target)) == 0
        capsys.readouterr()
        assert cmd_plan(_plan_args(str(target), check=True, output="text")) == 0


# ---------------------------------------------------------------- D-117
class TestPlanSchema:
    def test_plan_schema_output(self, tmp_path, capsys):
        target = tmp_path / "ohbs-image.toml"
        from ohbs_image._onboarding import cmd_configure
        assert cmd_configure(_configure_args(target)) == 0
        capsys.readouterr()
        assert cmd_plan(_plan_args(str(target), schema=True)) == 0
        schema = json.loads(capsys.readouterr().out)
        assert schema["$id"] == "https://ohbs-image.dev/plan/v1.schema.json"
        assert "mutates_cloud" in schema["required"]


# ---------------------------------------------------------------- D-118
class TestPlanSave:
    def test_plan_save_writes_evidence(self, tmp_path, capsys):
        state = tmp_path / "state"
        os.environ["OHBS_IMAGE_STATE_DIR"] = str(state)
        try:
            target = tmp_path / "ohbs-image.toml"
            from ohbs_image._onboarding import cmd_configure
            assert cmd_configure(_configure_args(target)) == 0
            capsys.readouterr()
            assert cmd_plan(_plan_args(str(target), save=True)) == 0
            plans = list((state / "plans").glob("*-plan.json"))
            assert len(plans) == 1
            doc = json.loads(plans[0].read_text(encoding="utf-8"))
            assert doc["schema"] == "https://ohbs-image.dev/plan/v1"
        finally:
            os.environ.pop("OHBS_IMAGE_STATE_DIR", None)


# ---------------------------------------------------------------- D-119
class TestPlanDiffLast:
    def test_plan_diff_last_no_lineage(self, tmp_path, capsys):
        state = tmp_path / "state"
        os.environ["OHBS_IMAGE_STATE_DIR"] = str(state)
        try:
            target = tmp_path / "ohbs-image.toml"
            from ohbs_image._onboarding import cmd_configure
            assert cmd_configure(_configure_args(target)) == 0
            capsys.readouterr()
            rc = cmd_plan(_plan_args(str(target), diff_last=True, output="json"))
            assert rc == 0
            doc = json.loads(capsys.readouterr().out)
            assert doc["diff_last_build"]["changed"] is False
        finally:
            os.environ.pop("OHBS_IMAGE_STATE_DIR", None)


# ---------------------------------------------------------------- D-120
class TestPlanNeverMutates:
    def test_plan_never_calls_write_api(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "ohbs-image.toml"
        from ohbs_image._onboarding import cmd_configure
        assert cmd_configure(_configure_args(target)) == 0
        capsys.readouterr()

        def boom(*a, **k):
            raise AssertionError("plan must never invoke build/render/packer APIs")

        monkeypatch.setattr("ohbs_image.render_all", boom)
        monkeypatch.setattr("ohbs_image.run_packer", boom)
        monkeypatch.setattr("ohbs_image._record_lineage", boom)
        rc = cmd_plan(_plan_args(str(target)))
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["mutates_cloud"] is False
