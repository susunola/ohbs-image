from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import ohbs_image

from ._config import load_config, load_config_layered, resolve
from ._logging import ConfigError, banner, fail, info, ok

LAUNCH_SCHEMA = "https://ohbs-image.dev/launch-result/v1"


def _invoke(argv: list[str], quiet_stdout: bool) -> int:
    if quiet_stdout:
        with redirect_stdout(io.StringIO()):
            return ohbs_image.main(argv)
    return ohbs_image.main(argv)


def _stage(result: dict[str, Any], name: str, argv: list[str], quiet_stdout: bool) -> int:
    rc = _invoke(argv, quiet_stdout)
    result["stages"].append({"name": name, "status": "passed" if rc == 0 else "failed",
                             "exit_code": rc})
    return rc


def _finish(result: dict[str, Any], output: str) -> None:
    if output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    status = str(result["status"]).upper()
    print(f"launch {status} — run {result['run_id']}")
    for stage in result["stages"]:
        print(f"  {stage['name']:10s} {stage['status']}")
    if result.get("next_action"):
        print(f"Next: {result['next_action']}")


def cmd_launch(args: argparse.Namespace) -> int:
    """Guide one config through diagnostics, planning, preflight and build."""
    if args.build and not args.yes:
        fail("launch --build requires --yes to explicitly approve billed cloud resources")
        return 2
    try:
        overlays = list(args.overlay or [])
        config = (load_config_layered([Path(args.config), *(Path(item) for item in overlays)])
                  if overlays else load_config(Path(args.config)))
        resolved = resolve(config)
    except ConfigError as exc:
        fail(str(exc))
        return 2

    run_id = ohbs_image._new_run_id()
    resolved.run_id = run_id
    result: dict[str, Any] = {
        "schema": LAUNCH_SCHEMA,
        "run_id": run_id,
        "status": "active",
        "profile": resolved.profile_name,
        "config": str(Path(args.config).expanduser()),
        "stages": [],
    }
    json_mode = args.output == "json"
    if not json_mode:
        banner("launch")
        info(f"Run ID: {run_id}")
        info("No billed resources are created before the build stage")

    common = ["--config", args.config, "--workdir", args.workdir]
    for overlay in overlays:
        common.extend(["--overlay", overlay])

    ohbs_image._write_run_manifest(
        resolved, status="active", phase="launch-doctor",
        next_action=f"ohbs-image launch --config {args.config}")
    doctor = ["doctor", *common]
    if args.offline:
        doctor.append("--offline")
    if _stage(result, "doctor", doctor, json_mode) != 0:
        result["status"] = "failed"
        result["next_action"] = f"ohbs-image doctor --config {args.config}"
        ohbs_image._write_run_manifest(
            resolved, status="failed", phase="launch-doctor",
            next_action=result["next_action"])
        _finish(result, args.output)
        return 1

    ohbs_image._write_run_manifest(resolved, status="active", phase="launch-plan")
    plan = ["plan", *common, "--check", "--save", "--run-id", run_id]
    if _stage(result, "plan", plan, json_mode) != 0:
        result["status"] = "failed"
        result["next_action"] = f"ohbs-image plan --config {args.config} --check"
        ohbs_image._write_run_manifest(
            resolved, status="failed", phase="launch-plan",
            next_action=result["next_action"])
        _finish(result, args.output)
        return 1

    ohbs_image._write_run_manifest(resolved, status="active", phase="launch-preflight")
    if _stage(result, "preflight", ["preflight", *common], json_mode) != 0:
        result["status"] = "failed"
        result["next_action"] = f"ohbs-image preflight --config {args.config}"
        ohbs_image._write_run_manifest(
            resolved, status="failed", phase="launch-preflight",
            next_action=result["next_action"])
        _finish(result, args.output)
        return 1

    if not args.build or args.dry_run:
        result["status"] = "ready"
        result["next_action"] = f"ohbs-image launch --config {args.config} --build --yes"
        ohbs_image._write_run_manifest(
            resolved, status="ready", phase="launch-ready",
            next_action=result["next_action"])
        _finish(result, args.output)
        return 0

    ohbs_image._write_run_manifest(resolved, status="active", phase="launch-build")
    build = ["build", *common, "--yes", "--run-id", run_id]
    if args.quiet:
        build.append("--quiet")
    if args.debug:
        build.append("--debug")
    if args.skip_if_unchanged:
        build.append("--skip-if-unchanged")
    if args.log_file:
        build.extend(["--log-file", args.log_file])
    if args.result_file:
        build.extend(["--result-file", args.result_file])
    if args.timeout:
        build.extend(["--timeout", str(args.timeout)])
    rc = _stage(result, "build", build, json_mode)
    result["status"] = "completed" if rc == 0 else "failed"
    result["next_action"] = (f"ohbs-image run show {run_id}" if rc == 0
                             else f"ohbs-image run show {run_id} --output json")
    if rc == 0:
        ok(f"Launch completed: {run_id}")
    _finish(result, args.output)
    return rc
