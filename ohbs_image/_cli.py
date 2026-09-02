from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable

import ohbs_image

from ._ancestry import (
    cmd_ancestry_descendants,
    cmd_ancestry_impact,
    cmd_ancestry_link,
    cmd_ancestry_revoke,
    cmd_ancestry_verify,
)
from ._audit import cmd_audit
from ._benchmark import cmd_benchmark_compare, cmd_benchmark_run
from ._build_checkpoints import cmd_run_checkpoints
from ._catalog_tools import cmd_catalog_list, cmd_catalog_verify
from ._channels import cmd_channel_list, cmd_channel_promote, cmd_channel_resolve
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
from ._compliance_pack import PROFILES as COMPLIANCE_PROFILES
from ._compliance_pack import cmd_compliance_assess
from ._config import _lineage_path
from ._config_tools import (
    cmd_config_diff,
    cmd_config_explain,
    cmd_config_get,
    cmd_config_merge,
    cmd_config_migrate,
    cmd_config_schema,
    cmd_config_validate,
)
from ._consumer import cmd_consumer_resolve
from ._cve_feed import OSV_QUERY_BATCH, cmd_cve_sync
from ._discover import cmd_discover
from ._distribution import (
    cmd_distribution_execute,
    cmd_distribution_plan,
    cmd_distribution_reconcile,
    cmd_distribution_record,
)
from ._distribution_controller import (
    cmd_distribution_enqueue,
    cmd_distribution_reconcile_all,
    cmd_distribution_slo,
    cmd_distribution_worker,
)
from ._dr import cmd_dr_drill
from ._engine import cmd_engine_list, cmd_engine_verify, cmd_engine_version
from ._extensions import ENTRY_POINT_GROUPS, cmd_extension_list, cmd_extension_verify
from ._guide import JOURNEYS, cmd_guide
from ._launch import cmd_launch, cmd_run_resume
from ._logging import VERSION, _setup_logging, disable_color, fail
from ._metrics import cmd_report_metrics, cmd_report_trends
from ._onboarding import DOCTOR_GROUPS, cmd_configure, cmd_doctor, cmd_plan, set_non_interactive
from ._policy import (
    cmd_policy_check,
    cmd_policy_exceptions,
    cmd_policy_explain,
    cmd_policy_verify,
)
from ._policy_registry import (
    cmd_policy_list,
    cmd_policy_publish,
    cmd_policy_resolve,
    cmd_policy_revoke,
)
from ._policy_simulation import cmd_policy_simulate
from ._profiles import DEFAULT_WORKDIR, PROFILE_NAMES_HELP, PROFILES
from ._proof import cmd_proof_record, cmd_proof_report, cmd_proof_verify
from ._providers import cmd_provider_list, cmd_provider_verify
from ._quickstart import cmd_quickstart
from ._rebuild_events import cmd_event_process
from ._reconcile import cmd_state_reconcile
from ._registry import (
    cmd_registry_list,
    cmd_registry_rebuild,
    cmd_registry_search,
    cmd_registry_show,
    cmd_registry_status,
    cmd_registry_verify,
)
from ._report_diff import cmd_report_cost, cmd_report_diff, cmd_report_html, cmd_report_list, cmd_report_show
from ._rule_quality import cmd_catalog_lint
from ._run_events import cmd_run_events
from ._runs import cmd_run_list, cmd_run_show
from ._service import cmd_serve
from ._slo import cmd_report_slo
from ._state import (
    cmd_state_init,
    cmd_state_path,
    cmd_state_prune,
    cmd_state_status,
    cmd_state_sync,
    cmd_state_verify,
)
from ._state_db import (
    cmd_state_db_backup,
    cmd_state_db_export,
    cmd_state_db_init,
    cmd_state_db_migrate,
    cmd_state_db_verify,
)
from ._telemetry import TraceRecorder
from ._try import cmd_try
from ._upgrade import cmd_upgrade_check
from ._worker import cmd_worker_run

# Roadmap D-91 — commands grouped by lifecycle in --help output.
# Roadmap (verify/cleanup convergence) — `verify` and `cleanup` are now
# subcommand groups; the flat legacy names stay registered as deprecated
# aliases (see _deprecated_alias below) and remain grouped here so --help
# renders them under the same lifecycle headings.
COMMAND_GROUPS: dict[str, list[str]] = {
    "start here": ["guide", "try", "launch", "quickstart"],
    "build lifecycle": [
        "init", "configure", "doctor", "discover", "plan", "preflight", "benchmark",
        "validate", "build", "scan", "test",
    ],
    "manage & evidence": [
        "run", "state", "config", "registry", "ancestry", "channel", "policy", "compliance", "consumer", "distribution", "event", "cve", "dr", "provider", "extension", "proof", "upgrade", "worker", "serve", "report", "catalog", "engine", "list", "images", "clean",
        "verify", "cleanup", "pending", "audit", "drift", "check-source",
        "verify-image", "cleanup-images", "cleanup-runs",
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

    p_guide = sub.add_parser(
        "guide", help="Choose a role-based path through the product")
    p_guide.add_argument("role", nargs="?", choices=tuple(JOURNEYS),
                         help="Show one journey (omit to list every role)")
    p_guide.add_argument("--output", choices=["text", "json"], default="text",
                         help="Output format (default: text)")
    p_guide.set_defaults(func=cmd_guide)

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
    p_doctor.add_argument("--support-bundle",
                          help="Create a redacted ZIP support bundle (refuses overwrite)")
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

    p_provider = sub.add_parser("provider", help="Inspect and verify cloud provider plugins")
    provider_sub = p_provider.add_subparsers(dest="provider_command")
    p_provider_list = provider_sub.add_parser("list", help="List installed providers and capabilities")
    p_provider_list.add_argument("--output", choices=["text", "json"], default="text")
    p_provider_list.set_defaults(func=cmd_provider_list)
    p_provider_verify = provider_sub.add_parser("verify", help="Verify provider API compatibility")
    p_provider_verify.add_argument("name")
    p_provider_verify.add_argument("--output", choices=["text", "json"], default="text")
    p_provider_verify.set_defaults(func=cmd_provider_verify)

    p_extension = sub.add_parser("extension", help="Discover and certify extension plugins")
    extension_sub = p_extension.add_subparsers(dest="extension_command")
    p_extension_list = extension_sub.add_parser("list", help="List installed extensions")
    p_extension_list.add_argument("--kind", choices=sorted(ENTRY_POINT_GROUPS))
    p_extension_list.set_defaults(func=cmd_extension_list)
    p_extension_verify = extension_sub.add_parser("verify", help="Run extension certification")
    p_extension_verify.add_argument("kind", choices=sorted(ENTRY_POINT_GROUPS))
    p_extension_verify.add_argument("name")
    p_extension_verify.set_defaults(func=cmd_extension_verify)

    p_proof = sub.add_parser("proof", help="Collect and verify long-running production evidence")
    proof_sub = p_proof.add_subparsers(dest="proof_command")
    p_proof_record = proof_sub.add_parser("record", help="Append one daily hash-chained snapshot")
    p_proof_record.add_argument("--ledger", default="")
    p_proof_record.add_argument("--size", type=int, default=1000)
    p_proof_record.set_defaults(func=cmd_proof_record)
    p_proof_verify = proof_sub.add_parser("verify", help="Verify the proof ledger hash chain")
    p_proof_verify.add_argument("--ledger", default="")
    p_proof_verify.set_defaults(func=cmd_proof_verify)
    p_proof_report = proof_sub.add_parser("report", help="Evaluate 30/90-day proof claims")
    p_proof_report.add_argument("--ledger", default="")
    p_proof_report.add_argument("--days", type=int, choices=[30, 90], default=30)
    p_proof_report.add_argument("--html", default="")
    p_proof_report.set_defaults(func=cmd_proof_report)

    p_compliance = sub.add_parser("compliance", help="Generate technical compliance evidence packs")
    compliance_sub = p_compliance.add_subparsers(dest="compliance_command")
    p_compliance_assess = compliance_sub.add_parser("assess", help="Map artifact evidence to controls")
    p_compliance_assess.add_argument("artifact_id")
    p_compliance_assess.add_argument("--profile", choices=COMPLIANCE_PROFILES, required=True)
    p_compliance_assess.add_argument("--output-dir", default="compliance-output")
    p_compliance_assess.set_defaults(func=cmd_compliance_assess)

    p_benchmark = sub.add_parser("benchmark", help="Run reproducible local performance benchmarks")
    benchmark_sub = p_benchmark.add_subparsers(dest="benchmark_command")
    p_benchmark_run = benchmark_sub.add_parser("run", help="Measure controller hot paths")
    p_benchmark_run.add_argument("--iterations", type=int, default=500)
    p_benchmark_run.add_argument("--warmups", type=int, default=50)
    p_benchmark_run.add_argument("--output", default="")
    p_benchmark_run.set_defaults(func=cmd_benchmark_run)
    p_benchmark_compare = benchmark_sub.add_parser("compare", help="Compare a result against a baseline")
    p_benchmark_compare.add_argument("current")
    p_benchmark_compare.add_argument("baseline")
    p_benchmark_compare.add_argument("--max-regression-percent", type=float, default=20.0)
    p_benchmark_compare.set_defaults(func=cmd_benchmark_compare)

    p_upgrade = sub.add_parser("upgrade", help="Preflight package and state compatibility")
    upgrade_sub = p_upgrade.add_subparsers(dest="upgrade_command")
    p_upgrade_check = upgrade_sub.add_parser("check", help="Check a target version before upgrading")
    p_upgrade_check.add_argument("target_version")
    p_upgrade_check.add_argument("--database", default="")
    p_upgrade_check.set_defaults(func=cmd_upgrade_check)

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

    p_qs = sub.add_parser(
        "quickstart",
        help="Provision temp build resources, write config, hand off to build")
    p_qs.add_argument("--profile", choices=sorted(PROFILES), default="tencentos3")
    p_qs.add_argument("--region", required=True)
    p_qs.add_argument("--zone", default="",
                      help="Zone (default: first available in the region)")
    p_qs.add_argument("--level", type=int, choices=[1, 2], default=1)
    p_qs.add_argument("--target", default="ohbs-image.toml",
                      help="Config to write (default ./ohbs-image.toml)")
    p_qs.add_argument("--force", action="store_true",
                      help="Overwrite an existing config")
    p_qs.add_argument("--vpc", default="",
                      help="Reuse an existing VPC (requires --subnet and "
                           "--security-group too)")
    p_qs.add_argument("--subnet", default="")
    p_qs.add_argument("--security-group", default="")
    p_qs.add_argument("--instance-type", default="",
                      help="Override the auto-selected instance type")
    p_qs.add_argument("--ingress-cidr", default="",
                      help="Runner public IPv4/CIDR allowed to reach SSH/WinRM; "
                           "required when provisioning temporary networking")
    p_qs.add_argument("--dry-run", action="store_true",
                      help="Read-only: print the plan, create nothing")
    p_qs.add_argument("--yes", action="store_true",
                      help="Chain build after doctor+plan")
    p_qs.add_argument("--cleanup", action="store_true",
                      help="Delete resources recorded by a previous quickstart")
    p_qs.set_defaults(func=cmd_quickstart)

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
    p_plan.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    p_plan.set_defaults(func=cmd_plan)

    p_launch = sub.add_parser(
        "launch", parents=[common],
        help="Guide doctor, plan, preflight and an explicitly approved build")
    p_launch.add_argument("--build", action="store_true",
                          help="Continue into the billed cloud build stage")
    p_launch.add_argument("--yes", action="store_true",
                          help="Explicitly approve billed resources (required with --build)")
    p_launch.add_argument("--offline", action="store_true",
                          help="Skip network checks in doctor (preflight still validates config)")
    p_launch.add_argument("--output", choices=["text", "json"], default="text")
    p_launch.add_argument("--quiet", action="store_true")
    p_launch.add_argument("--debug", action="store_true")
    p_launch.add_argument("--skip-if-unchanged", action="store_true")
    p_launch.add_argument("--log-file", default=None)
    p_launch.add_argument("--result-file", default=None)
    p_launch.set_defaults(func=cmd_launch)

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
    p_st_verify = state_sub.add_parser(
        "verify", help="Check evidence integrity, references, hashes and run leases")
    p_st_verify.add_argument("--output", choices=["text", "json"], default="text")
    p_st_verify.add_argument("--strict", action="store_true",
                             help="Treat warnings as verification failures")
    p_st_verify.set_defaults(func=cmd_state_verify)
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
    p_st_reconcile = state_sub.add_parser(
        "reconcile", help="Detect expired runs and recorded orphan resources")
    p_st_reconcile.add_argument("--apply", action="store_true",
                                help="Mark expired local runs failed; never deletes cloud resources")
    p_st_reconcile.add_argument("--output", choices=["text", "json"], default="text")
    p_st_reconcile.set_defaults(func=cmd_state_reconcile)
    p_st_db = state_sub.add_parser("db", help="Manage the transactional SQLite state backend")
    db_sub = p_st_db.add_subparsers(dest="state_db_command")
    for name, help_text, func in (
        ("init", "Initialize a durable WAL database", cmd_state_db_init),
        ("verify", "Verify database and object hashes", cmd_state_db_verify),
    ):
        db_parser = db_sub.add_parser(name, help=help_text)
        db_parser.add_argument("--database", default="")
        db_parser.set_defaults(func=func)
    p_db_migrate = db_sub.add_parser("migrate", help="Import legacy file state transactionally")
    p_db_migrate.add_argument("--database", default="")
    p_db_migrate.add_argument("--source", default="")
    p_db_migrate.add_argument("--apply", action="store_true")
    p_db_migrate.set_defaults(func=cmd_state_db_migrate)
    p_db_export = db_sub.add_parser("export", help="Export a reversible file-state snapshot")
    p_db_export.add_argument("destination")
    p_db_export.add_argument("--database", default="")
    p_db_export.add_argument("--force", action="store_true")
    p_db_export.set_defaults(func=cmd_state_db_export)
    p_db_backup = db_sub.add_parser("backup", help="Create an online consistent database backup")
    p_db_backup.add_argument("destination")
    p_db_backup.add_argument("--database", default="")
    p_db_backup.set_defaults(func=cmd_state_db_backup)

    p_run = sub.add_parser("run", help="Inspect unified run state and evidence")
    run_sub = p_run.add_subparsers(dest="run_command")
    p_run_list = run_sub.add_parser("list", help="List runs across every evidence source")
    p_run_list.add_argument("--limit", type=int, default=20)
    p_run_list.add_argument("--profile")
    p_run_list.add_argument("--status")
    p_run_list.add_argument("--output", choices=["text", "json"], default="text")
    p_run_list.set_defaults(func=cmd_run_list)
    p_run_show = run_sub.add_parser("show", help="Show all evidence associated with one run")
    p_run_show.add_argument("run_id")
    p_run_show.add_argument("--output", choices=["text", "json"], default="text")
    p_run_show.set_defaults(func=cmd_run_show)
    p_run_events = run_sub.add_parser("events", help="Show the immutable state-transition log")
    p_run_events.add_argument("run_id")
    p_run_events.add_argument("--output", choices=["text", "json"], default="text")
    p_run_events.set_defaults(func=cmd_run_events)
    p_run_checkpoints = run_sub.add_parser(
        "checkpoints", help="Show completed build phases and recoverable artifacts")
    p_run_checkpoints.add_argument("run_id")
    p_run_checkpoints.add_argument("--output", choices=["text", "json"], default="text")
    p_run_checkpoints.set_defaults(func=cmd_run_checkpoints)
    p_run_resume = run_sub.add_parser(
        "resume", help="Resume a failed launch from its last safe checkpoint")
    p_run_resume.add_argument("run_id")
    p_run_resume.add_argument("--build", action="store_true",
                              help="Continue into the billed cloud build stage")
    p_run_resume.add_argument("--yes", action="store_true",
                              help="Explicitly approve billed resources (required with --build)")
    p_run_resume.add_argument("--offline", action="store_true")
    p_run_resume.add_argument("--output", choices=["text", "json"], default="text")
    p_run_resume.add_argument("--quiet", action="store_true")
    p_run_resume.add_argument("--debug", action="store_true")
    p_run_resume.add_argument("--skip-if-unchanged", action="store_true")
    p_run_resume.add_argument("--log-file", default=None)
    p_run_resume.add_argument("--result-file", default=None)
    p_run_resume.add_argument("--timeout", type=int, default=None)
    p_run_resume.set_defaults(func=cmd_run_resume)

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
    p_html = report_sub.add_parser(
        "html", help="Re-render one run as a self-contained HTML compliance page")
    p_html.add_argument("run_id")
    p_html.add_argument("-o", "--output", default=None,
                        help="Write to PATH (default: "
                             "<state-dir>/reports/<image>.<run>.html)")
    p_html.set_defaults(func=cmd_report_html)
    p_cost = report_sub.add_parser(
        "cost", help="Aggregate build cost from lineage facts "
                     "(instance type, spot, duration)")
    p_cost.add_argument("--hourly-price", type=float, default=None,
                        help="On-demand USD/hour to estimate spend from "
                             "recorded durations (spot runs at 10%%)")
    p_cost.add_argument("--output", choices=["text", "json"], default="text")
    p_cost.set_defaults(func=cmd_report_cost)
    p_slo = report_sub.add_parser("slo", help="Summarize run reliability and latency SLOs")
    p_slo.add_argument("--days", type=int, default=30)
    p_slo.add_argument("--output", choices=["text", "json"], default="text")
    p_slo.set_defaults(func=cmd_report_slo)
    p_metrics = report_sub.add_parser(
        "metrics", help="Export run, registry, channel and replica metrics")
    p_metrics.add_argument("--days", type=int, default=30)
    p_metrics.add_argument("--format", choices=["prometheus", "otlp-json", "json"],
                           default="prometheus")
    p_metrics.add_argument("--record", action="store_true",
                           help="Persist this snapshot in the local trend database")
    p_metrics.add_argument("--push", default="", metavar="OTLP_ENDPOINT",
                           help="Push metrics to an OTLP/HTTP JSON endpoint")
    p_metrics.set_defaults(func=cmd_report_metrics)
    p_trends = report_sub.add_parser("trends", help="Query persisted metric history")
    p_trends.add_argument("--limit", type=int, default=100)
    p_trends.set_defaults(func=cmd_report_trends)

    p_registry = sub.add_parser("registry", help="Manage versioned golden-image artifacts")
    registry_sub = p_registry.add_subparsers(dest="registry_command")
    p_reg_list = registry_sub.add_parser("list", help="List registered artifacts")
    p_reg_list.add_argument("--bucket")
    p_reg_list.add_argument("--status", choices=["active", "quarantined", "revoked"])
    p_reg_list.add_argument("--output", choices=["text", "json"], default="text")
    p_reg_list.set_defaults(func=cmd_registry_list)
    p_reg_show = registry_sub.add_parser("show", help="Show one artifact")
    p_reg_show.add_argument("artifact_id")
    p_reg_show.add_argument("--output", choices=["text", "json"], default="text")
    p_reg_show.set_defaults(func=cmd_registry_show)
    p_reg_search = registry_sub.add_parser("search", help="Search the artifact database")
    p_reg_search.add_argument("--query", default="")
    p_reg_search.add_argument("--bucket", default="")
    p_reg_search.add_argument("--status", default="",
                              choices=["", "active", "quarantined", "revoked"])
    p_reg_search.add_argument("--version", default="")
    p_reg_search.add_argument("--label", default="", metavar="KEY=VALUE")
    p_reg_search.add_argument("--limit", type=int, default=100)
    p_reg_search.add_argument("--offset", type=int, default=0)
    p_reg_search.add_argument("--output", choices=["text", "json"], default="text")
    p_reg_search.set_defaults(func=cmd_registry_search)
    p_reg_rebuild = registry_sub.add_parser(
        "rebuild", help="Rebuild registry artifacts from release evidence")
    p_reg_rebuild.add_argument("--output", choices=["text", "json"], default="text")
    p_reg_rebuild.set_defaults(func=cmd_registry_rebuild)
    p_reg_verify = registry_sub.add_parser("verify", help="Verify registry hashes and identities")
    p_reg_verify.add_argument("--output", choices=["text", "json"], default="text")
    p_reg_verify.set_defaults(func=cmd_registry_verify)

    p_ancestry = sub.add_parser("ancestry", help="Query artifact lineage and blast radius")
    ancestry_sub = p_ancestry.add_subparsers(dest="ancestry_command")
    p_anc_desc = ancestry_sub.add_parser("descendants", help="List all downstream artifacts")
    p_anc_desc.add_argument("artifact_id")
    p_anc_desc.add_argument("--output", choices=["text", "json"], default="text")
    p_anc_desc.set_defaults(func=cmd_ancestry_descendants)
    p_anc_link = ancestry_sub.add_parser("link", help="Add a parent relationship")
    p_anc_link.add_argument("child")
    p_anc_link.add_argument("parent")
    p_anc_link.add_argument("--relation", default="derived_from")
    p_anc_link.add_argument("--external", action="store_true")
    p_anc_link.set_defaults(func=cmd_ancestry_link)
    p_anc_impact = ancestry_sub.add_parser("impact", help="Plan artifact and channel blast radius")
    p_anc_impact.add_argument("artifact_id")
    p_anc_impact.add_argument("--output", choices=["text", "json"], default="text")
    p_anc_impact.set_defaults(func=cmd_ancestry_impact)
    p_anc_revoke = ancestry_sub.add_parser(
        "revoke", help="Preview or apply descendant-first cascading revocation")
    p_anc_revoke.add_argument("artifact_id")
    p_anc_revoke.add_argument("--reason", required=True)
    p_anc_revoke.add_argument("--actor")
    p_anc_revoke.add_argument("--apply", action="store_true")
    p_anc_revoke.add_argument("--output", choices=["text", "json"], default="text")
    p_anc_revoke.set_defaults(func=cmd_ancestry_revoke)
    p_anc_verify = ancestry_sub.add_parser("verify", help="Detect cycles and malformed edges")
    p_anc_verify.add_argument("--output", choices=["text", "json"], default="text")
    p_anc_verify.set_defaults(func=cmd_ancestry_verify)
    for status in ("quarantine", "revoke"):
        stored_status = "quarantined" if status == "quarantine" else "revoked"
        p_status = registry_sub.add_parser(status, help=f"Mark an artifact {stored_status}")
        p_status.add_argument("artifact_id")
        p_status.add_argument("--reason", required=True)
        p_status.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown")
        p_status.add_argument("--no-auto-rollback", action="store_true")
        p_status.add_argument("--output", choices=["text", "json"], default="text")
        p_status.set_defaults(func=cmd_registry_status, artifact_status=stored_status)

    p_channel = sub.add_parser("channel", help="Atomically promote and resolve artifact channels")
    channel_sub = p_channel.add_subparsers(dest="channel_command")
    p_channel_promote = channel_sub.add_parser("promote", help="Atomically move a channel pointer")
    p_channel_promote.add_argument("bucket")
    p_channel_promote.add_argument("channel")
    p_channel_promote.add_argument("artifact_id")
    p_channel_promote.add_argument("--expected-generation", type=int)
    p_channel_promote.add_argument("--operation-id", help="Idempotency key for safe worker retries")
    p_channel_promote.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown")
    p_channel_promote.add_argument("--reason", default="")
    p_channel_promote.add_argument("--policy", help="Policy bundle to enforce before promotion")
    p_channel_promote.add_argument("--environment", help="Policy environment (default: channel name)")
    p_channel_promote.add_argument("--output", choices=["text", "json"], default="text")
    p_channel_promote.set_defaults(func=cmd_channel_promote)
    p_channel_resolve = channel_sub.add_parser("resolve", help="Resolve and verify a channel")
    p_channel_resolve.add_argument("bucket")
    p_channel_resolve.add_argument("channel")
    p_channel_resolve.add_argument("--output", choices=["text", "json"], default="text")
    p_channel_resolve.set_defaults(func=cmd_channel_resolve)
    p_channel_list = channel_sub.add_parser("list", help="List channel pointers")
    p_channel_list.add_argument("--bucket")
    p_channel_list.add_argument("--output", choices=["text", "json"], default="text")
    p_channel_list.set_defaults(func=cmd_channel_list)

    p_policy = sub.add_parser("policy", help="Verify and evaluate organization policy bundles")
    policy_sub = p_policy.add_subparsers(dest="policy_command")
    p_policy_verify = policy_sub.add_parser("verify", help="Validate a policy bundle and inheritance")
    p_policy_verify.add_argument("bundle")
    p_policy_verify.add_argument("--output", choices=["text", "json"], default="text")
    p_policy_verify.set_defaults(func=cmd_policy_verify)
    p_policy_check = policy_sub.add_parser("check", help="Evaluate an artifact for an environment")
    p_policy_check.add_argument("artifact_id")
    p_policy_check.add_argument("--bundle", required=True)
    p_policy_check.add_argument("--environment", required=True)
    p_policy_check.add_argument("--output", choices=["text", "json"], default="text")
    p_policy_check.set_defaults(func=cmd_policy_check)
    p_policy_explain = policy_sub.add_parser(
        "explain", help="Explain effective control sources and exception applicability"
    )
    p_policy_explain.add_argument("bundle")
    p_policy_explain.add_argument("--environment", default="production")
    p_policy_explain.add_argument("--artifact-id", default="")
    p_policy_explain.add_argument("--output", choices=["text", "json"], default="text")
    p_policy_explain.set_defaults(func=cmd_policy_explain)
    p_policy_exceptions = policy_sub.add_parser(
        "exceptions", help="Show exception expiry posture (active / expiring / expired)"
    )
    p_policy_exceptions.add_argument("bundle")
    p_policy_exceptions.add_argument(
        "--environment", default="", help="Restrict to exceptions in this environment"
    )
    p_policy_exceptions.add_argument(
        "--within-days", type=int, default=0,
        help="Flag exceptions expiring within this many days as 'expiring' (0 disables)"
    )
    p_policy_exceptions.add_argument(
        "--fail-on-expired", action="store_true",
        help="Exit 1 when any exception has already expired"
    )
    p_policy_exceptions.add_argument("--output", choices=["text", "json"], default="text")
    p_policy_exceptions.set_defaults(func=cmd_policy_exceptions)
    p_policy_simulate = policy_sub.add_parser(
        "simulate", help="Dry-run a candidate policy against registered artifacts"
    )
    p_policy_simulate.add_argument("bundle")
    p_policy_simulate.add_argument("--environment", required=True)
    p_policy_simulate.add_argument(
        "--baseline", default="",
        help="Currently active policy to diff against (reports newly allowed/denied)"
    )
    p_policy_simulate.add_argument(
        "--artifact", action="append", default=[],
        help="Simulate only this artifact (repeatable; default: every registered artifact)"
    )
    p_policy_simulate.add_argument(
        "--fail-on-newly-denied", action="store_true",
        help="Exit 1 when the candidate newly denies any artifact versus the baseline"
    )
    p_policy_simulate.add_argument("--output", choices=["text", "json"], default="text")
    p_policy_simulate.set_defaults(func=cmd_policy_simulate)
    p_policy_publish = policy_sub.add_parser("publish", help="Publish an immutable policy version")
    p_policy_publish.add_argument("bundle")
    p_policy_publish.add_argument("--actor", required=True)
    p_policy_publish.add_argument("--activate", action="store_true")
    p_policy_publish.set_defaults(func=cmd_policy_publish)
    p_policy_resolve = policy_sub.add_parser("resolve", help="Resolve an active or pinned policy")
    p_policy_resolve.add_argument("policy_id")
    p_policy_resolve.add_argument("--version", default="")
    p_policy_resolve.set_defaults(func=cmd_policy_resolve)
    p_policy_list = policy_sub.add_parser("list", help="List registered policy versions")
    p_policy_list.set_defaults(func=cmd_policy_list)
    p_policy_revoke = policy_sub.add_parser("revoke", help="Revoke a policy version")
    p_policy_revoke.add_argument("policy_id")
    p_policy_revoke.add_argument("version")
    p_policy_revoke.add_argument("--actor", required=True)
    p_policy_revoke.add_argument("--reason", required=True)
    p_policy_revoke.set_defaults(func=cmd_policy_revoke)

    p_consumer = sub.add_parser("consumer", help="Resolve deployable images for CI, Terraform and OPA")
    consumer_sub = p_consumer.add_subparsers(dest="consumer_command")
    p_consumer_resolve = consumer_sub.add_parser(
        "resolve", help="Resolve a channel and enforce an optional policy bundle")
    p_consumer_resolve.add_argument("bucket")
    p_consumer_resolve.add_argument("channel")
    p_consumer_resolve.add_argument("--policy")
    p_consumer_resolve.add_argument("--environment",
                                    help="Policy environment (default: channel name)")
    p_consumer_resolve.add_argument("--output", choices=["json", "terraform"], default="json")
    p_consumer_resolve.set_defaults(func=cmd_consumer_resolve)

    p_distribution = sub.add_parser(
        "distribution", help="Plan cached regional replication and record replicas")
    distribution_sub = p_distribution.add_subparsers(dest="distribution_command")
    p_dist_plan = distribution_sub.add_parser("plan", help="Plan copies without cloud writes")
    p_dist_plan.add_argument("artifact_id")
    p_dist_plan.add_argument("--region", action="append", required=True)
    p_dist_plan.add_argument("--output", choices=["text", "json"], default="text")
    p_dist_plan.set_defaults(func=cmd_distribution_plan)
    p_dist_record = distribution_sub.add_parser("record", help="Record a completed regional copy")
    p_dist_record.add_argument("artifact_id")
    p_dist_record.add_argument("--region", required=True)
    p_dist_record.add_argument("--replica-id", required=True)
    p_dist_record.add_argument("--operation-id", help="Idempotency key for safe worker retries")
    p_dist_record.set_defaults(func=cmd_distribution_record)
    p_dist_execute = distribution_sub.add_parser(
        "execute", help="Plan replication, or explicitly start cloud copies")
    p_dist_execute.add_argument("artifact_id")
    p_dist_execute.add_argument("--region", action="append", required=True)
    p_dist_execute.add_argument("--apply", action="store_true",
                                help="Call SyncImages (default: dry-run)")
    p_dist_execute.add_argument("--output", choices=["text", "json"], default="text")
    p_dist_execute.set_defaults(func=cmd_distribution_execute)
    p_dist_reconcile = distribution_sub.add_parser(
        "reconcile", help="Converge pending replicas using DescribeImages")
    p_dist_reconcile.add_argument("artifact_id")
    p_dist_reconcile.add_argument("--timeout-minutes", type=int, default=60)
    p_dist_reconcile.add_argument("--output", choices=["text", "json"], default="text")
    p_dist_reconcile.set_defaults(func=cmd_distribution_reconcile)
    p_dist_enqueue = distribution_sub.add_parser("enqueue", help="Queue a regional copy or share")
    p_dist_enqueue.add_argument("artifact_id")
    p_dist_enqueue.add_argument("--region", required=True)
    p_dist_enqueue.add_argument("--account", default="self")
    p_dist_enqueue.add_argument("--mode", choices=["sync", "share"], default="sync")
    p_dist_enqueue.add_argument("--database", default="")
    p_dist_enqueue.set_defaults(func=cmd_distribution_enqueue)
    p_dist_worker = distribution_sub.add_parser("worker", help="Run the quota-aware distribution worker")
    p_dist_worker.add_argument("--database", default="")
    p_dist_worker.add_argument("--apply", action="store_true")
    p_dist_worker.add_argument("--once", action="store_true")
    p_dist_worker.add_argument("--worker-id", default="")
    p_dist_worker.add_argument("--global-limit", type=int, default=8)
    p_dist_worker.add_argument("--account-limit", type=int, default=4)
    p_dist_worker.add_argument("--region-limit", type=int, default=2)
    p_dist_worker.add_argument("--max-attempts", type=int, default=3)
    p_dist_worker.add_argument("--poll-seconds", type=float, default=10)
    p_dist_worker.set_defaults(func=cmd_distribution_worker)
    p_dist_slo = distribution_sub.add_parser("slo", help="Report distribution propagation SLO")
    p_dist_slo.add_argument("--database", default="")
    p_dist_slo.add_argument("--target-minutes", type=int, default=30)
    p_dist_slo.set_defaults(func=cmd_distribution_slo)
    p_dist_reconcile_all = distribution_sub.add_parser(
        "reconcile-all", help="Periodically reconcile every pending replica")
    p_dist_reconcile_all.add_argument("--apply", action="store_true")
    p_dist_reconcile_all.add_argument("--once", action="store_true")
    p_dist_reconcile_all.add_argument("--interval-seconds", type=float, default=60)
    p_dist_reconcile_all.add_argument("--timeout-minutes", type=int, default=60)
    p_dist_reconcile_all.set_defaults(func=cmd_distribution_reconcile_all)

    p_event = sub.add_parser("event", help="Plan and process rebuild-triggering events")
    event_sub = p_event.add_subparsers(dest="event_command")
    p_event_process = event_sub.add_parser(
        "process", help="Plan impact or queue quarantined artifacts for rebuild")
    p_event_process.add_argument("event")
    p_event_process.add_argument("--apply", action="store_true",
                                 help="Quarantine affected artifacts and queue rebuilds")
    p_event_process.add_argument("--actor", default="event-controller")
    p_event_process.add_argument("--output", choices=["text", "json"], default="text")
    p_event_process.set_defaults(func=cmd_event_process)

    p_cve = sub.add_parser("cve", help="Synchronize production vulnerability feeds")
    cve_sub = p_cve.add_subparsers(dest="cve_command")
    p_cve_sync = cve_sub.add_parser("sync", help="Query OSV for artifact package inventories")
    p_cve_sync.add_argument("--inventory", required=True)
    p_cve_sync.add_argument("--state", default=".ohbs-state/cve-feed-state.json")
    p_cve_sync.add_argument("--endpoint", default=OSV_QUERY_BATCH)
    p_cve_sync.add_argument("--timeout", type=int, default=30)
    p_cve_sync.add_argument("--apply", action="store_true")
    p_cve_sync.add_argument("--output", choices=["text", "json"], default="text")
    p_cve_sync.set_defaults(func=cmd_cve_sync)

    p_worker = sub.add_parser("worker", help="Process queued rebuild requests")
    worker_sub = p_worker.add_subparsers(dest="worker_command")
    p_worker_run = worker_sub.add_parser("run", help="Run the fenced rebuild worker")
    worker_handler = p_worker_run.add_mutually_exclusive_group(required=True)
    worker_handler.add_argument("--handler",
                                help="Command that reads request JSON and returns result JSON")
    worker_handler.add_argument("--pipeline",
                                help="Built-in four-stage rebuild pipeline JSON")
    p_worker_run.add_argument("--apply", action="store_true",
                              help="Claim and execute requests (default: dry-run)")
    p_worker_run.add_argument("--once", action="store_true")
    p_worker_run.add_argument("--worker-id", default="")
    p_worker_run.add_argument("--max-attempts", type=int, default=3)
    p_worker_run.add_argument("--lease-seconds", type=int, default=900)
    p_worker_run.add_argument("--retry-delay-seconds", type=int, default=60)
    p_worker_run.add_argument("--poll-seconds", type=float, default=5.0)
    p_worker_run.add_argument("--timeout", type=int, default=7200)
    p_worker_run.add_argument("--state-db", default="",
                              help="Use the transactional SQLite queue backend")
    p_worker_run.set_defaults(func=cmd_worker_run)

    p_dr = sub.add_parser("dr", help="Run isolated disaster-recovery and chaos drills")
    dr_sub = p_dr.add_subparsers(dest="dr_command")
    p_dr_drill = dr_sub.add_parser("drill", help="Verify recovery without touching live state")
    p_dr_drill.add_argument("--scenario", choices=["all", "database", "worker", "evidence"],
                            default="all")
    p_dr_drill.add_argument("--output", default="", help="Write the JSON evidence report")
    p_dr_drill.set_defaults(func=cmd_dr_drill)

    p_serve = sub.add_parser("serve", help="Run the authenticated HTTP control plane")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8181)
    p_serve.add_argument("--rbac", required=True, help="Bearer-token RBAC JSON configuration")
    p_serve.add_argument("--allow-remote", action="store_true",
                         help="Allow non-loopback listen (put behind TLS)")
    p_serve.add_argument("--max-body-bytes", type=int, default=1_048_576)
    p_serve.add_argument("--request-timeout", type=int, default=30)
    p_serve.add_argument("--rate-limit", type=int, default=120,
                         help="Authenticated requests per token per window; 0 disables")
    p_serve.add_argument("--rate-window-seconds", type=int, default=60)
    p_serve.set_defaults(func=cmd_serve)

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
    p_clint = catalog_sub.add_parser(
        "lint", help="Score CIS rule quality and detect semantic conflicts"
    )
    p_clint.add_argument("--strict", action="store_true",
                         help="Fail on Automated/manual semantic conflicts")
    p_clint.add_argument("--profile", choices=sorted(PROFILES), default="")
    p_clint.add_argument("--output", choices=["text", "json"], default="text")
    p_clint.add_argument("--report", default="", metavar="PATH",
                         help="Write a self-contained HTML quality baseline")
    p_clint.set_defaults(func=cmd_catalog_lint)

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
    p_bld.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    p_bld.add_argument("--capacity-plan", default="",
                       help="Select the first purchasable placement from a fallback plan")
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

    p_verify = sub.add_parser(
        "verify", parents=[common],
        help="Verify provenance signatures, images or release manifests")
    # The flat `verify` form (no subcommand) remains the group's default and
    # is deprecated: the legacy flags live on the group parser so
    # `ohbs-image verify --provenance x` still works, while
    # `ohbs-image verify provenance` is the forward path.
    p_verify.add_argument("--provenance", default=None,
                          help="Path to a .provenance.json file")
    p_verify.add_argument("--image", default=None,
                          help="Image ID to look up its provenance (e.g. img-xxx)")
    p_verify.add_argument("--trusted-key-fingerprint", action="append", default=[],
                          help="Require signer fingerprint (40 hex chars); repeat for an allowlist")
    p_verify.set_defaults(func=_deprecated_alias(
        "verify", "verify provenance", cmd_verify))
    verify_sub = p_verify.add_subparsers(dest="verify_command")
    p_vrf_prov = verify_sub.add_parser(
        "provenance", parents=[common],
        help="Verify a SLSA provenance signature")
    p_vrf_prov.add_argument("--provenance", default=None,
                            help="Path to a .provenance.json file")
    p_vrf_prov.add_argument("--image", default=None,
                            help="Image ID to look up its provenance (e.g. img-xxx)")
    p_vrf_prov.add_argument("--trusted-key-fingerprint", action="append", default=[],
                            help="Require signer fingerprint (40 hex chars); repeat for an allowlist")
    p_vrf_prov.set_defaults(func=cmd_verify)

    p_vrf_img = verify_sub.add_parser(
        "image", parents=[common],
        help="Clean-boot verification: boot a probe from a produced image, "
             "re-audit on fresh boot via SSH/WinRM, terminate")
    p_vrf_img.add_argument("--image", required=True,
                           help="Image ID to verify (e.g. img-xxxx)")
    p_vrf_img.add_argument("--min-score", type=float, default=85.0,
                           help="Gate threshold in percent (default 85)")
    p_vrf_img.set_defaults(func=cmd_verify_image)

    p_vrf_rel = verify_sub.add_parser(
        "release", parents=[common],
        help="Verify an approved image's release-manifest evidence")
    p_vrf_rel.add_argument("--image", required=True,
                           help="Approved image ID (e.g. img-xxxx)")
    p_vrf_rel.set_defaults(func=cmd_verify_release)

    # Deprecated flat aliases of the verify group. These names are frozen in
    # contracts/core-contracts.json, and removing a top-level command requires a
    # new major contract version (docs/core-contract-stability.md), so they are
    # scheduled for removal in 1.0.0 rather than a minor release. They keep
    # parsing identically, but warn and point at the group form.
    p_vrf_img_alias = sub.add_parser(
        "verify-image", parents=[common],
        help="[deprecated] use 'ohbs-image verify image'")
    p_vrf_img_alias.add_argument("--image", required=True,
                                 help="Image ID to verify (e.g. img-xxxx)")
    p_vrf_img_alias.add_argument("--min-score", type=float, default=85.0,
                                 help="Gate threshold in percent (default 85)")
    p_vrf_img_alias.set_defaults(func=_deprecated_alias(
        "verify-image", "verify image", cmd_verify_image))

    p_vrf_rel_alias = sub.add_parser(
        "verify-release", parents=[common],
        help="[deprecated] use 'ohbs-image verify release'")
    p_vrf_rel_alias.add_argument("--image", required=True,
                                 help="Approved image ID (e.g. img-xxxx)")
    p_vrf_rel_alias.set_defaults(func=_deprecated_alias(
        "verify-release", "verify release", cmd_verify_release))

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
    p_scn.add_argument("--html", default=None,
                       help="Write a self-contained HTML compliance report to PATH")
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

    p_try = sub.add_parser(
        "try", help="Zero-cost offline demo: engine gates + a sample HTML report")
    p_try.add_argument("-o", "--output", default="./ohbs-image-try",
                       help="Directory for demo artifacts (default ./ohbs-image-try)")
    p_try.add_argument("--profile", default="tencentos3",
                       help="Profile to demo (default tencentos3)")
    p_try.add_argument("--level", type=int, choices=[1, 2], default=1,
                       help="CIS level for the sample report (default 1)")
    p_try.set_defaults(func=cmd_try)

    p_cleanup = sub.add_parser(
        "cleanup",
        help="Retire old golden images or orphaned probe CVMs")
    cleanup_sub = p_cleanup.add_subparsers(dest="cleanup_command")
    p_clnimg = cleanup_sub.add_parser(
        "images",
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

    p_clnruns = cleanup_sub.add_parser(
        "runs", parents=[common],
        help="Retire tagged orphaned build/probe CVMs (dry-run by default)")
    p_clnruns.add_argument("--older-than", type=int, default=24,
                           help="Terminate tagged ephemeral CVMs older than N hours (default 24)")
    p_clnruns.add_argument("--include-legacy", action="store_true",
                           help="Also select pre-manifest probe instances (requires explicit opt-in)")
    p_clnruns.add_argument("--apply", action="store_true", help="Actually terminate instances")
    p_clnruns.set_defaults(func=cmd_cleanup_runs)

    # Deprecated flat aliases of the cleanup group (scheduled for removal in
    # 1.0.0 — see the verify aliases above for why not a minor release): keep
    # parsing identically, but warn and point at the group form.
    p_clnimg_alias = sub.add_parser(
        "cleanup-images",
        help="[deprecated] use 'ohbs-image cleanup images'")
    p_clnimg_alias.add_argument("--older-than", type=int, default=30,
                                help="Delete builds older than N days (default 30)")
    p_clnimg_alias.add_argument("--keep-latest", type=int, default=1,
                                help="Keep the newest N builds (default 1)")
    p_clnimg_alias.add_argument("--unused-since", type=int, default=0,
                                help="Only delete images NOT shared with other "
                                     "accounts (in-use guard); 0 = off")
    p_clnimg_alias.add_argument("--apply", action="store_true",
                                help="Actually delete images (default is a dry run)")
    p_clnimg_alias.set_defaults(func=_deprecated_alias(
        "cleanup-images", "cleanup images", cmd_cleanup_images))

    p_clnruns_alias = sub.add_parser("cleanup-runs", parents=[common],
                                     help="[deprecated] use 'ohbs-image cleanup runs'")
    p_clnruns_alias.add_argument("--older-than", type=int, default=24,
                                 help="Terminate tagged ephemeral CVMs older than N hours (default 24)")
    p_clnruns_alias.add_argument("--include-legacy", action="store_true",
                                 help="Also select pre-manifest probe instances (requires explicit opt-in)")
    p_clnruns_alias.add_argument("--apply", action="store_true",
                                 help="Actually terminate instances")
    p_clnruns_alias.set_defaults(func=_deprecated_alias(
        "cleanup-runs", "cleanup runs", cmd_cleanup_runs))

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
        command = " ".join((argv or sys.argv[1:])[:2]) or "help"
        recorder = TraceRecorder(_lineage_path().parent)
        with recorder.span("cli.command", attributes={"command": command}) as span:
            result = int(args.func(args))
            span["attributes"]["exit_code"] = result
            return result
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


def _deprecated_alias(alias: str, replacement: str,
                      func: Callable[[argparse.Namespace], int]
                      ) -> Callable[[argparse.Namespace], int]:
    """Wrap a legacy flat command as a deprecated alias.

    verify/cleanup convergence — the flat forms (`verify-image`,
    `verify-release`, `cleanup-images`, `cleanup-runs`, and the flat `verify`
    default) still work, but print a removal-window notice to stderr before
    dispatching to the real handler. Scheduled for removal in 1.0.0.
    """
    def _wrapped(args: argparse.Namespace) -> int:
        print(f"warning: '{alias}' is deprecated, use 'ohbs-image {replacement}' "
              "(scheduled for removal in 1.0.0)", file=sys.stderr)
        return func(args)
    return _wrapped


def _deprecation_prog(argv: list[str] | None) -> None:
    """Roadmap D-92/93 — keep the pre-rebrand entry name as a deprecated alias.

    `cis-image` (the pre-0.16.25 package name) still works but prints a
    deprecation notice; it is scheduled for removal in 1.0.0.
    """
    first = argv[0] if argv else sys.argv[0]
    name = os.path.basename(str(first)).lower()
    if name in ("cis-image", "cis_image"):
        print("warning: 'cis-image' is deprecated, use 'ohbs-image' "
              "(scheduled for removal in 1.0.0)", file=sys.stderr)
