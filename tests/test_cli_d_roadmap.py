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
- verify/cleanup subcommand-group convergence (flat legacy names stay
  registered as deprecated aliases, removal window 0.22.0)
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
from ohbs_image._cli import (
    COMMAND_GROUPS,
    _deprecated_alias,
    _deprecation_prog,
)
from ohbs_image._commands import (
    cmd_cleanup_images,
    cmd_cleanup_runs,
    cmd_verify,
    cmd_verify_image,
    cmd_verify_release,
)
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
        assert "cis-image" in err and "deprecated" in err and "0.22.0" in err

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


# ---------------------------------------------- verify/cleanup convergence
class TestVerifyCleanupGroups:
    """`verify` and `cleanup` are subcommand groups. The flat legacy names
    remain registered as deprecated aliases (removal window 0.22.0), and the
    bare `verify` (no subcommand) is the group default — still parsing its
    legacy flags, but printing a deprecation notice before dispatch."""

    def test_verify_group_subcommands_parse_and_route(self):
        parser = build_parser()
        ns = parser.parse_args(["verify", "provenance", "--provenance", "p.json"])
        assert ns.command == "verify"
        assert ns.verify_command == "provenance"
        assert ns.provenance == "p.json"
        # subparser defaults must override the group default — the bare
        # `verify` alias wrapper must NOT swallow the real handlers
        assert ns.func is cmd_verify

        ns = parser.parse_args(["verify", "image", "--image", "img-1", "--min-score", "90"])
        assert ns.verify_command == "image"
        assert ns.image == "img-1"
        assert ns.min_score == 90
        assert ns.func is cmd_verify_image

        ns = parser.parse_args(["verify", "release", "--image", "img-1"])
        assert ns.verify_command == "release"
        assert ns.image == "img-1"
        assert ns.func is cmd_verify_release

    def test_bare_verify_is_group_default_and_wrapped(self):
        parser = build_parser()
        ns = parser.parse_args(["verify", "--provenance", "p.json"])
        assert ns.command == "verify"
        assert ns.verify_command is None          # no subcommand chosen
        assert ns.provenance == "p.json"          # legacy flag still parses
        assert ns.func is not cmd_verify          # wrapped: warns + dispatches

    def test_cleanup_group_subcommands_parse_and_route(self):
        parser = build_parser()
        ns = parser.parse_args(["cleanup", "images", "--older-than", "10", "--apply"])
        assert ns.command == "cleanup"
        assert ns.cleanup_command == "images"
        assert ns.older_than == 10
        assert ns.apply is True
        assert ns.func is cmd_cleanup_images

        ns = parser.parse_args(["cleanup", "runs", "--older-than", "48", "--include-legacy"])
        assert ns.cleanup_command == "runs"
        assert ns.older_than == 48
        assert ns.include_legacy is True
        assert ns.func is cmd_cleanup_runs

    def test_deprecated_flat_aliases_parse_and_route_through_wrapper(self):
        parser = build_parser()
        ns = parser.parse_args(["verify-image", "--image", "img-1"])
        assert ns.command == "verify-image"
        assert ns.image == "img-1"
        assert ns.func is not cmd_verify_image    # wrapped

        ns = parser.parse_args(["verify-release", "--image", "img-1"])
        assert ns.command == "verify-release"
        assert ns.func is not cmd_verify_release  # wrapped

        ns = parser.parse_args(["cleanup-images", "--older-than", "10"])
        assert ns.command == "cleanup-images"
        assert ns.older_than == 10
        assert ns.func is not cmd_cleanup_images  # wrapped

        ns = parser.parse_args(["cleanup-runs", "--older-than", "24"])
        assert ns.command == "cleanup-runs"
        assert ns.older_than == 24
        assert ns.func is not cmd_cleanup_runs    # wrapped

    def test_help_renders_groups_and_deprecated_markers(self):
        text = build_parser().format_help()
        for name in ("verify", "verify-image", "verify-release",
                     "cleanup", "cleanup-images", "cleanup-runs"):
            assert f"    {name}" in text, f"{name} missing from grouped help"
        assert "[deprecated] use 'ohbs-image verify image'" in text
        assert "[deprecated] use 'ohbs-image verify release'" in text
        assert "[deprecated] use 'ohbs-image cleanup images'" in text
        assert "[deprecated] use 'ohbs-image cleanup runs'" in text


class TestDeprecatedAliasWrapper:
    """_deprecated_alias() is the shared wrapper for the converged verify/
    cleanup flat names: it warns on stderr with the replacement and the
    0.22.0 removal window, then dispatches to the real handler."""

    def test_warns_on_stderr_with_replacement_and_window(self, capsys):
        calls = []

        def fake(args):
            calls.append(args)
            return 42

        wrapped = _deprecated_alias("verify-image", "verify image", fake)
        assert wrapped(argparse.Namespace(marker=True)) == 42
        assert len(calls) == 1 and calls[0].marker is True   # dispatch intact
        captured = capsys.readouterr()
        assert captured.out == ""                            # never on stdout
        assert "verify-image" in captured.err
        assert "deprecated" in captured.err
        assert "ohbs-image verify image" in captured.err
        assert "0.22.0" in captured.err

    def test_bare_verify_and_cleanup_aliases_share_the_wrapper(self, capsys):
        parser = build_parser()
        for argv in (["verify", "--provenance", "x.json"],
                     ["verify-image", "--image", "img-1"],
                     ["verify-release", "--image", "img-1"],
                     ["cleanup-images"],
                     ["cleanup-runs", "--older-than", "24"]):
            ns = parser.parse_args(argv)
            assert callable(ns.func)
            assert ns.func.__name__ == "_wrapped", argv


# ---------------------------------------------------------------- py3.14 help validation
def _iter_subparsers(parser):
    """Yield the child parsers registered under *parser* (any nesting)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.values()


class TestHelpStringsValid:
    """argparse on Python 3.14 validates help strings at add_argument time
    (``_check_help`` expands %-format eagerly, without the auto-escape
    ``_get_help_string`` applies on <=3.13), so any literal ``%`` in a help
    string must be written as ``%%``. Walk the whole parser tree and fail on
    bare percent signs, then render every parser's help to prove it works."""

    def test_no_bare_percent_in_any_help_string(self):
        import re

        parser = build_parser()

        def walk(p, seen):
            for action in p._actions:
                if not action.help:
                    continue
                # "%%" is the argparse escape for a literal "%"; "%(name)s"
                # is a legal placeholder. Anything else is a bare "%" that
                # py3.14 rejects at add_argument time.
                cleaned = re.sub(r"%%|%\([^)]*\)s", "", action.help)
                if "%" in cleaned:
                    where = action.option_strings or [action.dest or "?"]
                    raise AssertionError(
                        f"bare % in help of {where}: {action.help!r}"
                    )
            for child in _iter_subparsers(p):
                if child not in seen:
                    seen.add(child)
                    walk(child, seen)

        walk(parser, set())

    def test_full_help_renders(self):
        # Exercising format_help() forces every help string through argparse's
        # %-expansion — the exact path py3.14 validates eagerly. Render every
        # parser in the tree so no subparser action escapes the check.
        parser = build_parser()

        def walk(p, seen):
            text = p.format_help()
            assert text, f"empty help for {p.prog}"
            for child in _iter_subparsers(p):
                if child not in seen:
                    seen.add(child)
                    walk(child, seen)

        walk(parser, set())
        report = next(c for c in _iter_subparsers(parser)
                      if c.prog.rsplit(" ", 1)[-1] == "report")
        cost = next(c for c in _iter_subparsers(report)
                    if c.prog.rsplit(" ", 1)[-1] == "cost")
        assert "--hourly-price" in cost.format_help()
        assert "10%)" in cost.format_help()
