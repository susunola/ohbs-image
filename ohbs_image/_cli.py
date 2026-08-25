from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable

import ohbs_image

from ._audit import cmd_audit
from ._catalog_tools import cmd_catalog_list, cmd_catalog_verify
from ._commands import (
    cmd_build,
    cmd_check_source,
    cmd_clean,
    cmd_cleanup_images,
    cmd_cleanup_runs,
    cmd_drift,
    cmd_images,
    cmd_init,
    cmd_list,
    cmd_pending,
    cmd_preflight,
    cmd_promote,
    cmd_rollback,
    cmd_scan,
    cmd_test,
    cmd_validate,
    cmd_verify,
    cmd_verify_image,
    cmd_verify_release,
)
from ._config_tools import (
    cmd_config_diff,
    cmd_config_explain,
    cmd_config_get,
    cmd_config_merge,
    cmd_config_migrate,
    cmd_config_schema,
    cmd_config_validate,
)
from ._discover import cmd_discover
from ._engine import cmd_engine_list, cmd_engine_verify, cmd_engine_version
from ._logging import VERSION, _setup_logging, disable_color, fail
from ._onboarding import DOCTOR_GROUPS, cmd_configure, cmd_doctor, cmd_plan, set_non_interactive
from ._profiles import DEFAULT_WORKDIR, PROFILE_NAMES_HELP, PROFILES
from ._report_diff import cmd_report_diff, cmd_report_list, cmd_report_show
from ._state import cmd_state_init, cmd_state_path, cmd_state_prune, cmd_state_status, cmd_state_sync

# Roadmap D-91 — commands grouped by lifecycle in --help output.
COMMAND_GROUPS: dict[str, list[str]] = {
    "build lifecycle": [
        "init", "configure", "doctor", "discover", "plan", "preflight",
        "validate", "build", "scan", "test",
    ],
    "manage & evidence": [
        "state", "config", "report", "catalog", "engine", "list", "images", "clean",
        "cleanup-images", "cleanup-runs", "pending", "audit", "drift",
        "check-source", "verify", "verify-image",
    ],
    "release": ["promote", "rollback", "verify-release"],
}


class GroupedHelpFormatter(argparse.HelpFormatter):
    """Render subcommands under lifecycle headings (roadmap D-91)."""

    def _format_action(self, action: argparse.Action) -> str:
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        # `add_parser(help=...)` text lives on the pseudo-actions, not on
        # the subparsers themselves (which only expose `description`).
        help_by_name = {act.dest: act.help
                        for act in action._choices_actions}
        parts: list[str] = []
        for group, names in COMMAND_GROUPS.items():
            rows = []
            for name in names:
                sub = action._name_parser_map.get(name)
                if sub is None:
                    continue
                rows.append((name, help_by_name.get(name, "") or ""))
            if not rows:
                continue
            parts.append(f"\n  {group}:")
            for name, help_text in rows:
                parts.append(f"    {name:<16} {help_text}")
        return "\n".join(parts) + "\n"

    def _format_usage(self, usage: str | None,
                      actions: Iterable[argparse.Action],
                      groups: Iterable[argparse._MutuallyExclusiveGroup],
                      prefix: str | None) -> str:
        return super()._format_usage(
            "ohbs-image COMMAND [options]", actions, groups, prefix)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="ohbs-image.toml",
                        help="Path to config file (default ./ohbs-image.toml)")
    common.add_argument("--overlay", action="append", default=None, metavar="TOML",
                        help="Additional config file layered on top of --config "
                             "(repeatable; later files override earlier ones "
                             "key-by-key — roadmap E)")
    common.add_argument("--workdir", default=DEFAULT_WORKDIR,
                        help=f"Rendered working directory (default ./{DEFAULT_WORKDIR})")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        help="Evidence state directory (default $OHBS_IMAGE_STATE_DIR or ~/.ohbs-image)")
    common.add_argument("--timeout", type=int, default=None, metavar="MINUTES",
                        help="Override the build wall-clock limit (overrides "
                             "[build].max_build_minutes); roadmap D-98")
    common.add_argument("--dry-run", action="store_true",
                        help="Render and report, but never invoke Packer or any "
                             "write API (roadmap D-99)")

    parser = argparse.ArgumentParser(
        prog="ohbs-image",
        description="ohbs-hardened Golden Image Builder (Packer × Tencent Cloud CVM)",
        epilog=f"Supported profiles: {PROFILE_NAMES_HELP}",
        formatter_class=GroupedHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ohbs-image {VERSION}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress info output; show warnings and errors only")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI colors (equivalent to NO_COLOR=1)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Never prompt; use defaults and fail on ambiguity")
    parser.add_argument("--state-dir", default=None,
                        help="Evidence state directory (may be placed before any command)")

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Generate sample ohbs-image.toml")
    p_init.add_argument("--target", default=".", help="Output directory (default: current)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", parents=[common],
                              help="Diagnose local toolchain, configuration and cloud access")
    p_doctor.add_argument("--output", choices=["text", "json", "sarif"], default="text")
    p_doctor.add_argument("--only", choices=DOCTOR_GROUPS, default="all",
                          help="Only run checks in this diagnostic group")
    p_doctor.add_argument("--offline", action="store_true",
                          help="Skip every network/cloud check (same as --no-cloud, plus no clock sync)")
    p_doctor.add_argument("--report-path",
                          help="Write a redacted diagnostic report to this file (text/json/sarif)")
    p_doctor.add_argument("--no-cloud", action="store_true",
                          help="Skip read-only Tencent Cloud API checks")
    p_doctor.set_defaults(func=cmd_doctor)

    p_discover = sub.add_parser("discover", help="Discover compatible Tencent Cloud resources")
    p_discover.add_argument("resource", choices=["images", "vpcs", "subnets", "security-groups", "instance-types"])
    p_discover.add_argument("--region", required=True)
    p_discover.add_argument("--zone", help="Required for instance-types")
    p_discover.add_argument("--vpc", help="Limit subnet discovery to this VPC")
    p_discover.add_argument("--profile", choices=sorted(PROFILES))
    p_discover.add_argument("--min-cpu", type=int, default=0,
                            help="instance-types: minimum vCPU count")
    p_discover.add_argument("--min-mem", type=int, default=0,
                            help="instance-types: minimum memory in GiB")
    p_discover.add_argument("--in-stock", action="store_true",
                            help="instance-types: only list types with available stock")
    p_discover.add_argument("--output", choices=["text", "json"], default="text")
    p_discover.set_defaults(func=cmd_discover)

    p_configure = sub.add_parser("configure", help="Generate a minimal build configuration")
    p_configure.add_argument("--target", default="ohbs-image.toml")
    p_configure.add_argument("--force", action="store_true")
    p_configure.add_argument("--discover", action="store_true",
                             help="Discover missing resources using read-only cloud APIs")
    p_configure.add_argument("--profile", choices=sorted(PROFILES))
    p_configure.add_argument("--region")
    p_configure.add_argument("--zone")
    p_configure.add_argument("--source-image")
    p_configure.add_argument("--vpc")
    p_configure.add_argument("--subnet")
    p_configure.add_argument("--security-group")
    p_configure.add_argument("--instance-type")
    p_configure.add_argument("--level", type=int, choices=[1, 2], default=1)
    p_configure.add_argument("--public-ip", action="store_true")
    p_configure.add_argument("--edit", action="store_true",
                             help="Open the generated config in $VISUAL/$EDITOR")
    p_configure.set_defaults(func=cmd_configure)

    p_plan = sub.add_parser("plan", parents=[common],
                            help="Preview resources, gates, duration and outputs without changes")
    p_plan.add_argument("--output", choices=["text", "json"], default="text",
                        help="Output format (json keeps the plan/v1 contract)")
    p_plan.add_argument("--check", action="store_true",
                        help="CI gate: exit non-zero when the plan contains "
                             "high-risk settings (roadmap D-116)")
    p_plan.add_argument("--save", action="store_true",
                        help="Persist the plan as evidence under the state "
                             "directory (roadmap D-118)")
    p_plan.add_argument("--diff-last", action="store_true",
                        help="Compare inputs against the last recorded build "
                             "(roadmap D-119)")
    p_plan.add_argument("--schema", action="store_true",
                        help="Print the plan JSON Schema and exit (roadmap D-117)")
    p_plan.set_defaults(func=cmd_plan)

    p_state = sub.add_parser("state", help="Inspect, initialize and synchronize evidence state")
    state_sub = p_state.add_subparsers(dest="state_command")
    p_sync = state_sub.add_parser("sync", help="Push or pull the evidence directory")
    p_sync.add_argument("direction", choices=["push", "pull"])
    p_sync.add_argument("--backend", choices=["local", "cos"], required=True)
    p_sync.add_argument("--location", required=True,
                        help="Local directory or cos://bucket/prefix")
    p_sync.add_argument("--check", action="store_true",
                        help="Preview transfers without copying (local backend only)")
    p_sync.set_defaults(func=cmd_state_sync)
    p_st_path = state_sub.add_parser("path", help="Print the evidence state directory")
    p_st_path.set_defaults(func=cmd_state_path)
    p_st_status = state_sub.add_parser(
        "status", help="Summarize evidence counts and disk usage")
    p_st_status.add_argument("--output", choices=["text", "json"], default="text")
    p_st_status.set_defaults(func=cmd_state_status)
    p_st_init = state_sub.add_parser(
        "init", help="Create the evidence directory layout (idempotent)")
    p_st_init.set_defaults(func=cmd_state_init)
    p_st_prune = state_sub.add_parser(
        "prune", help="Retain recent lineage, drop superseded per-run evidence")
    p_st_prune.add_argument("--keep", type=int, default=0,
                            help="Keep only the newest N lineage records (0 = no limit)")
    p_st_prune.add_argument("--older-than", type=int, default=0,
                            help="Drop records older than N days (0 = disabled)")
    p_st_prune.add_argument("--dry-run", action="store_true",
                            help="Preview what would be removed without changing anything")
    p_st_prune.add_argument("--output", choices=["text", "json"], default="text")
    p_st_prune.set_defaults(func=cmd_state_prune)

    p_config = sub.add_parser("config", help="Inspect, validate and migrate configuration contracts")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_schema = config_sub.add_parser("schema", help="Print the JSON Schema")
    p_schema.add_argument("--output")
    p_schema.set_defaults(func=cmd_config_schema)
    p_explain = config_sub.add_parser("explain", help="Explain configuration keys")
    p_explain.add_argument("key", nargs="?", help="Dotted key (e.g. build.max_build_minutes)")
    p_explain.add_argument("--all", action="store_true",
                           help="List every documented key, grouped by section")
    p_explain.set_defaults(func=cmd_config_explain)
    p_validate_cfg = config_sub.add_parser(
        "validate", help="Validate a config file locally (no cloud access)")
    p_validate_cfg.add_argument("--config", default="ohbs-image.toml")
    p_validate_cfg.add_argument("--output", choices=["text", "json"], default="text")
    p_validate_cfg.set_defaults(func=cmd_config_validate)
    p_diff_cfg = config_sub.add_parser(
        "diff", help="Field-level diff of two config files (before/after)")
    p_diff_cfg.add_argument("before")
    p_diff_cfg.add_argument("after")
    p_diff_cfg.add_argument("--output", choices=["text", "json"], default="text")
    p_diff_cfg.set_defaults(func=cmd_config_diff)
    p_get = config_sub.add_parser(
        "get", help="Print the effective value of one configuration key")
    p_get.add_argument("key", help="Dotted key (e.g. build.max_build_minutes)")
    p_get.add_argument("--config", default="ohbs-image.toml")
    p_get.add_argument("--output", choices=["text", "json"], default="text")
    p_get.set_defaults(func=cmd_config_get)
    p_migrate = config_sub.add_parser("migrate", help="Migrate legacy configuration to schema v1")
    p_migrate.add_argument("--config", default="ohbs-image.toml")
    p_migrate.add_argument("--output")
    p_migrate.add_argument("--apply", action="store_true",
                           help="Atomically update --config in place")
    p_migrate.set_defaults(func=cmd_config_migrate)
    p_merge = config_sub.add_parser(
        "merge", help="Deep-merge layered config files and validate the result")
    p_merge.add_argument("base", help="Base configuration file")
    p_merge.add_argument("overlays", nargs="+",
                         help="Overlay files (later files override earlier ones)")
    p_merge.add_argument("--output",
                         help="Write the merged TOML to this file (default: print)")
    p_merge.add_argument("--output-json", action="store_true", dest="output_json",
                         help="Emit a JSON validity report instead of merged TOML")
    p_merge.set_defaults(func=cmd_config_merge)

    p_report = sub.add_parser("report", help="Compare and inspect build evidence")
    report_sub = p_report.add_subparsers(dest="report_command")
    p_diff = report_sub.add_parser("diff", help="Compare two lineage run IDs")
    p_diff.add_argument("--before", required=True)
    p_diff.add_argument("--after", required=True)
    p_diff.add_argument("--output", choices=["text", "json"], default="text")
    p_diff.set_defaults(func=cmd_report_diff)
    p_list = report_sub.add_parser(
        "list", help="List lineage records (newest first) with filters")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--profile", help="Filter by profile name")
    p_list.add_argument("--status", choices=["ok", "failed"],
                        help="Filter by run status")
    p_list.add_argument("--mode", choices=["build", "scan", "test"],
                        help="Filter by run mode")
    p_list.add_argument("--output", choices=["text", "json"], default="text")
    p_list.set_defaults(func=cmd_report_list)
    p_show = report_sub.add_parser(
        "show", help="Show one run's evidence summary (lineage + manifest)")
    p_show.add_argument("run_id")
    p_show.add_argument("--output", choices=["text", "json"], default="text")
    p_show.set_defaults(func=cmd_report_show)

    p_engine = sub.add_parser("engine", help="Inspect and verify the bundled hardening engines")
    engine_sub = p_engine.add_subparsers(dest="engine_command")
    p_elist = engine_sub.add_parser(
        "list", help="Enumerate bundled engines per profile (version + sha256)")
    p_elist.add_argument("--output", choices=["text", "json"], default="text")
    p_elist.set_defaults(func=cmd_engine_list)
    p_everify = engine_sub.add_parser(
        "verify", help="Syntax-check every bundled engine (CI-ready)")
    p_everify.add_argument("--output", choices=["text", "json"], default="text")
    p_everify.set_defaults(func=cmd_engine_verify)
    p_ev = engine_sub.add_parser(
        "version", help="Print ohbs-image and per-family engine versions")
    p_ev.set_defaults(func=cmd_engine_version)

    p_catalog = sub.add_parser("catalog", help="Inspect and verify the bundled rule catalogs")
    catalog_sub = p_catalog.add_subparsers(dest="catalog_command")
    p_clist = catalog_sub.add_parser(
        "list", help="Enumerate bundled catalogs (rules, guidance, sha256)")
    p_clist.add_argument("--output", choices=["text", "json"], default="text")
    p_clist.set_defaults(func=cmd_catalog_list)
    p_cverify = catalog_sub.add_parser(
        "verify", help="Validate catalog JSON + guidance cross-references (CI-ready)")
    p_cverify.add_argument("--strict", action="store_true",
                           help="Fail on guidance cross-reference drift "
                                "(default: report as warnings)")
    p_cverify.add_argument("--output", choices=["text", "json"], default="text")
    p_cverify.set_defaults(func=cmd_catalog_verify)

    p_pre = sub.add_parser("preflight", parents=[common], help="Run pre-flight checks")
    p_pre.set_defaults(func=cmd_preflight)

    p_val = sub.add_parser("validate", parents=[common], help="Render + packer validate")
    p_val.add_argument("--quiet", action="store_true",
                       help="Suppress packer output (show only the ohbs-image summary)")
    p_val.add_argument("--debug", action="store_true",
                       help="Enable Packer debug logging (PACKER_LOG=1)")
    p_val.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="Enable debug logging (same as the global -v)")
    p_val.set_defaults(func=cmd_validate)

    p_bld = sub.add_parser("build", parents=[common], help="Render + packer build (produce image)")
    p_bld.add_argument("--quiet", action="store_true",
                       help="Suppress packer output (show only the ohbs-image summary)")
    p_bld.add_argument("--debug", action="store_true",
                       help="Enable Packer debug logging (PACKER_LOG=1)")
    p_bld.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="Enable debug logging (same as the global -v)")
    p_bld.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_bld.add_argument("--log-file", default=None,
                       help="Write full build log to file (in addition to stderr)")
    p_bld.add_argument("--result-file", default=None,
                       help="Write the machine-readable build result JSON to PATH")
    p_bld.add_argument("--skip-if-unchanged", action="store_true",
                       help="Skip the rebuild when inputs (source image, rules, "
                            "benchmark, level) are unchanged since the last "
                            "successful build")
    p_bld.set_defaults(func=cmd_build)

    p_cln = sub.add_parser("clean", parents=[common], help="Remove working directory")
    p_cln.set_defaults(func=cmd_clean)

    p_img = sub.add_parser("images", parents=[common],
                           help="List recorded builds (image lineage)")
    p_img.add_argument("--latest", action="store_true", help="Show only the newest record")
    p_img.add_argument("-n", "--limit", type=int, default=10,
                       help="Max records to show (default 10; 0 = all)")
    p_img.set_defaults(func=cmd_images)

    for command, handler, summary in (
        ("promote", cmd_promote, "Record approved-image promotion to an environment"),
        ("rollback", cmd_rollback, "Record rollback of an image from an environment"),
    ):
        release = sub.add_parser(command, parents=[common], help=summary)
        release.add_argument("--image", required=True, help="Approved image ID (e.g. img-xxxx)")
        release.add_argument("--environment", required=True,
                             help="Deployment environment name (e.g. staging or production)")
        release.add_argument("--approved-by", default="",
                             help="Human or CI identity to record (default GITHUB_ACTOR/USER)")
        release.add_argument("--reason", default="", help="Optional change or rollback reason")
        release.set_defaults(func=handler)

    p_verify_release = sub.add_parser("verify-release", parents=[common],
                                      help="Verify an approved image's release-manifest evidence")
    p_verify_release.add_argument("--image", required=True, help="Approved image ID (e.g. img-xxxx)")
    p_verify_release.set_defaults(func=cmd_verify_release)

    p_vrf = sub.add_parser("verify", parents=[common],
                           help="Verify a SLSA provenance signature")
    p_vrf.add_argument("--provenance", default=None,
                       help="Path to a .provenance.json file")
    p_vrf.add_argument("--image", default=None,
                       help="Image ID to look up its provenance (e.g. img-xxx)")
    p_vrf.add_argument("--trusted-key-fingerprint", action="append", default=[],
                       help="Require signer fingerprint (40 hex chars); repeat for an allowlist")
    p_vrf.set_defaults(func=cmd_verify)

    p_lst = sub.add_parser("list", help="Enumerate available profiles with metadata")
    p_lst.add_argument("--versions", action="store_true",
                       help="Show rule-catalog sha256 + engine version per profile")
    p_lst.add_argument("--output", choices=["text", "json"], default="text",
                       help="Output format (json keeps the list/v1 contract)")
    p_lst.set_defaults(func=cmd_list)

    p_scn = sub.add_parser("scan", parents=[common], help="Audit-only build (no remediation) with score gate")
    p_scn.add_argument("--quiet", action="store_true",
                       help="Suppress packer output (show only the ohbs-image summary)")
    p_scn.add_argument("--debug", action="store_true",
                       help="Enable Packer debug logging (PACKER_LOG=1)")
    p_scn.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="Enable debug logging (same as the global -v)")
    p_scn.add_argument("--min-score", type=float, default=85.0,
                       help="Gate threshold in percent (default 85)")
    p_scn.add_argument("--sarif", default=None,
                       help="Write a SARIF 2.1.0 report of failed rules to PATH")
    p_scn.add_argument("--xccdf", default=None,
                       help="Write an XCCDF 1.2 TestResult report of failed rules to PATH")
    p_scn.set_defaults(func=cmd_scan)

    p_pnd = sub.add_parser("pending", parents=[common],
                           help="Change detection: report whether a rebuild is needed")
    p_pnd.set_defaults(func=cmd_pending)

    p_aud = sub.add_parser(
        "audit",
        help="Independent third-party audit (oscap / inspec / kitty) with a score gate")
    p_aud.add_argument("--tool", choices=["oscap", "inspec", "kitty"], required=True,
                       help="Audit tool: oscap (SCAP content) | inspec (dev-sec baseline) | kitty (Windows CSV)")
    p_aud.add_argument("--host", default=None, help="Target host to audit (required for oscap/inspec)")
    p_aud.add_argument("--ssh-user", default="root", help="SSH user for oscap/inspec (default root)")
    p_aud.add_argument("--ssh-port", type=int, default=22, help="SSH port (default 22)")
    p_aud.add_argument("--ssh-key", default=None, help="SSH private key path (default: agent/keys)")
    p_aud.add_argument("--profile", default="xccdf_org.ssgproject.content_profile_cis",
                       help="oscap profile id (default: CIS profile in scap-security-guide)")
    p_aud.add_argument("--datastream", default=None,
                       help="oscap SCAP datastream path on the target (e.g. /usr/share/xml/scap/ssg/content/ssg-rhel9-ds.xml)")
    p_aud.add_argument("--baseline", default=None,
                       help="inspec baseline (default dev-sec/linux-baseline)")
    p_aud.add_argument("--parse", default=None,
                       help="kitty: path to a HardeningKitty audit CSV export to parse")
    p_aud.add_argument("--min-score", type=float, default=85.0,
                       help="Gate threshold in percent (default 85)")
    p_aud.add_argument("--sarif", default=None, help="Write findings as SARIF 2.1.0 to PATH")
    p_aud.add_argument("--xccdf", default=None, help="Write findings as XCCDF 1.2 to PATH")
    p_aud.set_defaults(func=cmd_audit)

    p_vrf_img = sub.add_parser(
        "verify-image", parents=[common],
        help="Clean-boot verification: boot a probe from a produced image, "
             "re-audit on fresh boot via SSH/WinRM, terminate")
    p_vrf_img.add_argument("--image", required=True,
                           help="Image ID to verify (e.g. img-xxxx)")
    p_vrf_img.add_argument("--min-score", type=float, default=85.0,
                           help="Gate threshold in percent (default 85)")
    p_vrf_img.set_defaults(func=cmd_verify_image)

    p_drift = sub.add_parser(
        "drift", parents=[common],
        help="Detect configuration drift on a running instance vs the image baseline")
    p_drift.add_argument("--host", required=True,
                         help="IP of the running instance to check for drift")
    p_drift.add_argument("--image", default="",
                         help="Producing image ID (baseline from its shipped audit; optional)")
    p_drift.add_argument("--baseline", default="",
                         help="Path to a saved baseline JSON (overrides --image)")
    p_drift.add_argument("--ssh-user", default="", help="SSH user (default: profile's)")
    p_drift.add_argument("--ssh-port", type=int, default=0, help="SSH port (default: profile's)")
    p_drift.add_argument("--save-baseline", action="store_true",
                         help="Save the current host scan as a baseline and exit")
    p_drift.set_defaults(func=cmd_drift)

    p_src = sub.add_parser(
        "check-source", parents=[common],
        help="Vendor image refresh detection — is the source image newer "
             "than the last build?")
    p_src.set_defaults(func=cmd_check_source)

    p_tst = sub.add_parser("test", parents=[common], help="Test the build pipeline")
    p_tst.add_argument("--quiet", action="store_true",
                       help="Suppress packer output (show only the ohbs-image summary)")
    p_tst.add_argument("--debug", action="store_true",
                       help="Enable Packer debug logging (PACKER_LOG=1)")
    p_tst.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="Enable debug logging (same as the global -v)")
    p_tst.add_argument("--idempotency", action="store_true",
                       help="Re-run apply and fail if the second pass makes changes")
    p_tst.set_defaults(func=cmd_test)

    p_clnimg = sub.add_parser(
        "cleanup-images",
        help="Retire old golden images by lineage age (dry-run by default)")
    p_clnimg.add_argument("--older-than", type=int, default=30,
                          help="Delete builds older than N days (default 30)")
    p_clnimg.add_argument("--keep-latest", type=int, default=1,
                          help="Keep the newest N builds (default 1)")
    p_clnimg.add_argument("--unused-since", type=int, default=0,
                          help="Only delete images NOT shared with other "
                               "accounts (in-use guard); 0 = off")
    p_clnimg.add_argument("--apply", action="store_true",
                          help="Actually delete images (default is a dry run)")
    p_clnimg.set_defaults(func=cmd_cleanup_images)

    p_clnruns = sub.add_parser("cleanup-runs", parents=[common],
                               help="Retire tagged orphaned build/probe CVMs (dry-run by default)")
    p_clnruns.add_argument("--older-than", type=int, default=24,
                           help="Terminate tagged ephemeral CVMs older than N hours (default 24)")
    p_clnruns.add_argument("--include-legacy", action="store_true",
                           help="Also select pre-manifest probe instances (requires explicit opt-in)")
    p_clnruns.add_argument("--apply", action="store_true", help="Actually terminate instances")
    p_clnruns.set_defaults(func=cmd_cleanup_runs)

    return parser

def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, dispatch to subcommand, return exit code."""
    _deprecation_prog(argv)
    _setup_logging()
    parser = ohbs_image.build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if getattr(args, "state_dir", None):
        os.environ["OHBS_IMAGE_STATE_DIR"] = args.state_dir
    if getattr(args, "no_color", False):
        disable_color()
    if getattr(args, "non_interactive", False):
        set_non_interactive()
    _setup_logging(verbose=getattr(args, "verbose", False),
                   quiet=getattr(args, "quiet", False))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(file=sys.stderr)
        fail("interrupted")
        return 130
    except Exception as exc:  # unexpected internal error: fail cleanly, show the traceback only under -v
        import traceback as _tb
        if getattr(args, "verbose", False):
            _tb.print_exc(file=sys.stderr)
        fail(f"internal error: {type(exc).__name__}: {exc} "
             "(rerun with -v for the full traceback)")
        return 70


def _deprecation_prog(argv: list[str] | None) -> None:
    """Roadmap D-92/93 — keep the pre-rebrand entry name as a deprecated alias.

    `cis-image` (the pre-0.16.25 package name) still works but prints a
    deprecation notice; it is scheduled for removal in 0.19.0.
    """
    first = argv[0] if argv else sys.argv[0]
    name = os.path.basename(str(first)).lower()
    if name in ("cis-image", "cis_image"):
        print("warning: 'cis-image' is deprecated, use 'ohbs-image' "
              "(scheduled for removal in 0.19.0)", file=sys.stderr)
