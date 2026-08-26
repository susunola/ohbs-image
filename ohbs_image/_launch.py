from __future__ import annotations

import argparse
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import ohbs_image

from ._config import load_config, load_config_layered, resolve
from ._logging import ConfigError, banner, fail, info, ok
from ._run_events import verify_event_chain

LAUNCH_SCHEMA = "https://ohbs-image.dev/launch-result/v1"


def _config_fingerprint(config: str, overlays: list[str]) -> str:
    digest = hashlib.sha256()
    for item in [config, *overlays]:
        path = Path(item).expanduser().resolve()
        digest.update(str(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _checkpoint(args: argparse.Namespace, completed: list[str]) -> dict[str, Any]:
    overlays = list(args.overlay or [])
    return {
        "version": 1,
        "workflow": "launch",
        "config": str(Path(args.config).expanduser().resolve()),
        "overlays": [str(Path(item).expanduser().resolve()) for item in overlays],
        "config_fingerprint": _config_fingerprint(args.config, overlays),
        "workdir": str(Path(args.workdir).expanduser().resolve()),
        "completed_stages": completed,
    }


def cmd_run_resume(args: argparse.Namespace) -> int:
    """Resume a launch workflow at its first incomplete safe checkpoint."""
    manifest = ohbs_image._read_run_manifest(args.run_id)
    if not manifest:
        fail(f"No run manifest for {args.run_id}")
        return 1
    checkpoint = manifest.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("workflow") != "launch":
        fail(f"Run {args.run_id} has no resumable launch checkpoint")
        return 1
    chain_failures = verify_event_chain(args.run_id)
    if chain_failures:
        fail("Run event chain is invalid; refusing resume: " + "; ".join(chain_failures))
        return 1
    state = str(manifest.get("state") or "")
    if state not in {"FAILED", "TIMED_OUT", "CANCELLED", "READY"}:
        fail(f"Run {args.run_id} cannot resume from state {state or 'unknown'}")
        return 1
    config = str(checkpoint.get("config") or "")
    overlays = [str(item) for item in checkpoint.get("overlays", [])]
    try:
        actual = _config_fingerprint(config, overlays)
    except OSError as exc:
        fail(f"Cannot read checkpoint configuration: {exc}")
        return 1
    if actual != checkpoint.get("config_fingerprint"):
        fail("Configuration changed since the checkpoint; start a new run")
        return 1
    resume_args = argparse.Namespace(
        config=config, overlay=overlays, workdir=str(checkpoint.get("workdir") or ".build"),
        build=args.build, yes=args.yes, offline=args.offline, output=args.output,
        quiet=args.quiet, debug=args.debug, dry_run=False,
        skip_if_unchanged=args.skip_if_unchanged, log_file=args.log_file,
        result_file=args.result_file, timeout=args.timeout, resume_run_id=args.run_id,
        resume_completed=list(checkpoint.get("completed_stages", [])),
    )
    return cmd_launch(resume_args)


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

    run_id = vars(args).get("resume_run_id") or ohbs_image._new_run_id()
    resolved.run_id = run_id
    completed = list(vars(args).get("resume_completed") or [])
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

    if vars(args).get("resume_run_id"):
        state_for_resume = (ohbs_image._read_run_manifest(run_id) or {}).get("state")
        if state_for_resume in {"FAILED", "TIMED_OUT", "CANCELLED"}:
            ohbs_image._write_run_manifest(
                resolved, status="active", phase="launch-retry",
                checkpoint=_checkpoint(args, completed))
    doctor = ["doctor", *common]
    if args.offline:
        doctor.append("--offline")
    if "doctor" not in completed:
        ohbs_image._write_run_manifest(
            resolved, status="active", phase="launch-doctor",
            next_action=f"ohbs-image launch --config {args.config}",
            checkpoint=_checkpoint(args, completed))
        if _stage(result, "doctor", doctor, json_mode) != 0:
            result["status"] = "failed"
            result["next_action"] = f"ohbs-image run resume {run_id}"
            ohbs_image._write_run_manifest(
                resolved, status="failed", phase="launch-doctor",
                next_action=result["next_action"], checkpoint=_checkpoint(args, completed))
            _finish(result, args.output)
            return 1

    if "doctor" not in completed:
        completed.append("doctor")
    plan = ["plan", *common, "--check", "--save", "--run-id", run_id]
    if "plan" not in completed:
        ohbs_image._write_run_manifest(resolved, status="active", phase="launch-plan",
                                       checkpoint=_checkpoint(args, completed))
        if _stage(result, "plan", plan, json_mode) != 0:
            result["status"] = "failed"
            result["next_action"] = f"ohbs-image run resume {run_id}"
            ohbs_image._write_run_manifest(
                resolved, status="failed", phase="launch-plan",
                next_action=result["next_action"], checkpoint=_checkpoint(args, completed))
            _finish(result, args.output)
            return 1

    if "plan" not in completed:
        completed.append("plan")
    if "preflight" not in completed:
        ohbs_image._write_run_manifest(resolved, status="active", phase="launch-preflight",
                                       checkpoint=_checkpoint(args, completed))
        if _stage(result, "preflight", ["preflight", *common], json_mode) != 0:
            result["status"] = "failed"
            result["next_action"] = f"ohbs-image run resume {run_id}"
            ohbs_image._write_run_manifest(
                resolved, status="failed", phase="launch-preflight",
                next_action=result["next_action"], checkpoint=_checkpoint(args, completed))
            _finish(result, args.output)
            return 1

    if "preflight" not in completed:
        completed.append("preflight")
    if not args.build or args.dry_run:
        result["status"] = "ready"
        result["next_action"] = f"ohbs-image run resume {run_id} --build --yes"
        ohbs_image._write_run_manifest(
            resolved, status="ready", phase="launch-ready",
            next_action=result["next_action"], checkpoint=_checkpoint(args, completed))
        _finish(result, args.output)
        return 0

    ohbs_image._write_run_manifest(resolved, status="active", phase="launch-build",
                                   checkpoint=_checkpoint(args, completed))
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
