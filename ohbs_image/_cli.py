from __future__ import annotations

import argparse
import os
import sys

import ohbs_image

from ._audit import cmd_audit
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
from ._config_tools import cmd_config_explain, cmd_config_migrate, cmd_config_schema
from ._discover import cmd_discover
from ._logging import VERSION, _setup_logging, fail
from ._onboarding import DOCTOR_GROUPS, cmd_configure, cmd_doctor, cmd_plan
from ._profiles import DEFAULT_WORKDIR, PROFILE_NAMES_HELP, PROFILES
from ._report_diff import cmd_report_diff
from ._state import cmd_state_sync


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="ohbs-image.toml",
                        help="Path to config file (default ./ohbs-image.toml)")
    common.add_argument("--workdir", default=DEFAULT_WORKDIR,
                        help=f"Rendered working directory (default ./{DEFAULT_WORKDIR})")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        help="Evidence state directory (default $OHBS_IMAGE_STATE_DIR or ~/.ohbs-image)")

    parser = argparse.ArgumentParser(
        prog="ohbs-image",
        description="ohbs-hardened Golden Image Builder (Packer × Tencent Cloud CVM)",
        epilog=f"Supported profiles: {PROFILE_NAMES_HELP}",
    )
    parser.add_argument("--version", action="version", version=f"ohbs-image {VERSION}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
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
    p_configure.set_defaults(func=cmd_configure)

    p_plan = sub.add_parser("plan", parents=[common],
                            help="Preview resources, gates, duration and outputs without changes")
    p_plan.add_argument("--output", choices=["text", "json"], default="text")
    p_plan.set_defaults(func=cmd_plan)

    p_state = sub.add_parser("state", help="Synchronize team evidence state")
    state_sub = p_state.add_subparsers(dest="state_command")
    p_sync = state_sub.add_parser("sync", help="Push or pull the evidence directory")
    p_sync.add_argument("direction", choices=["push", "pull"])
    p_sync.add_argument("--backend", choices=["local", "cos"], required=True)
    p_sync.add_argument("--location", required=True,
                        help="Local directory or cos://bucket/prefix")
    p_sync.set_defaults(func=cmd_state_sync)

    p_config = sub.add_parser("config", help="Inspect and migrate configuration contracts")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_schema = config_sub.add_parser("schema", help="Print the JSON Schema")
    p_schema.add_argument("--output")
    p_schema.set_defaults(func=cmd_config_schema)
    p_explain = config_sub.add_parser("explain", help="Explain one configuration key")
    p_explain.add_argument("key")
    p_explain.set_defaults(func=cmd_config_explain)
    p_migrate = config_sub.add_parser("migrate", help="Migrate legacy configuration to schema v1")
    p_migrate.add_argument("--config", default="ohbs-image.toml")
    p_migrate.add_argument("--output")
    p_migrate.add_argument("--apply", action="store_true",
                           help="Atomically update --config in place")
    p_migrate.set_defaults(func=cmd_config_migrate)

    p_report = sub.add_parser("report", help="Compare build evidence")
    report_sub = p_report.add_subparsers(dest="report_command")
    p_diff = report_sub.add_parser("diff", help="Compare two lineage run IDs")
    p_diff.add_argument("--before", required=True)
    p_diff.add_argument("--after", required=True)
    p_diff.add_argument("--output", choices=["text", "json"], default="text")
    p_diff.set_defaults(func=cmd_report_diff)

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
    _setup_logging()
    parser = ohbs_image.build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if getattr(args, "state_dir", None):
        os.environ["OHBS_IMAGE_STATE_DIR"] = args.state_dir
    _setup_logging(verbose=args.verbose)
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
