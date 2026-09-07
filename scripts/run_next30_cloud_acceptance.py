#!/usr/bin/env python3
"""Plan or execute the billed Tencent Cloud acceptance matrix.

Execution is deliberately triple-gated: --execute, --yes, and
OHBS_ALLOW_BILLED_TESTS=1. The default only writes a machine-readable plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from ohbs_image._benchmark import phase_benchmark
from ohbs_image._config import load_config, load_config_layered, resolve
from ohbs_image._tc_cloud import _create_temporary_ingress, _delete_temporary_ingress

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "tests" / "golden-matrix-tencent-linux.json"
SCHEMA = "https://ohbs-image.dev/next30-cloud-acceptance-plan/v1"
SUMMARY_SCHEMA = "https://ohbs-image.dev/next30-cloud-acceptance-summary/v1"
PREFLIGHT_SCHEMA = "https://ohbs-image.dev/next30-cloud-acceptance-preflight/v1"
MATRIX_SCHEMA = "https://ohbs-image.dev/golden-matrix/v1"
REQUIRED_CREDENTIALS = ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY")


def _config_identity(config: Path) -> dict[str, Any]:
    with config.open("rb") as stream:
        data = tomllib.load(stream)
    build = data.get("build") or {}
    ohbs = data.get("ohbs") or {}
    meta = data.get("meta") or {}
    return {
        "profile": build.get("profile"),
        "level": ohbs.get("level"),
        "source_image_id": build.get("source_image_id"),
        "benchmark": meta.get("benchmark"),
        "region": build.get("region"),
        "zone": build.get("zone"),
        "instance_type": build.get("instance_type"),
    }


def _validate_target_config(target: dict[str, Any], config: Path) -> None:
    expected = {key: target.get(key) for key in (
        "profile", "level", "source_image_id", "benchmark", "region", "zone",
        "instance_type",
    )}
    actual = _config_identity(config)
    drift = {
        key: {"matrix": expected[key], "config": actual[key]}
        for key in expected if expected[key] != actual[key]
    }
    if drift:
        detail = ", ".join(
            f"{key}: matrix={values['matrix']!r}, config={values['config']!r}"
            for key, values in drift.items()
        )
        raise ValueError(f"acceptance config drift in {config.relative_to(ROOT)}: {detail}")


def _env_file_names(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    names: set[str] = set()
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            names.add(match.group(1))
    return names


def offline_preflight(plan: dict[str, Any], *, output_root: Path,
                      env_file: Path | None = None) -> dict[str, Any]:
    """Check local billed-run readiness without validating credentials via an API."""
    file_names = _env_file_names(env_file)
    credentials = {
        name: {
            "present": bool(os.environ.get(name)) or name in file_names,
            "source": "environment" if os.environ.get(name) else
                      ("env_file" if name in file_names else "missing"),
        }
        for name in REQUIRED_CREDENTIALS
    }
    config_checks = []
    seen: set[str] = set()
    for job in plan.get("jobs") or []:
        config_path = Path(job["config"])
        if str(config_path) in seen:
            continue
        seen.add(str(config_path))
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        build = config.get("build") or {}
        missing_network = [name for name in ("vpc_id", "subnet_id", "security_group_id")
                           if not str(build.get(name) or "").strip()]
        config_checks.append({
            "config": str(config_path), "network_ids_present": not missing_network,
            "missing_network_ids": missing_network,
            "associate_public_ip": build.get("associate_public_ip") is True,
        })
    usage = shutil.disk_usage(output_root)
    checks = {
        "credentials_named": all(row["present"] for row in credentials.values()),
        "configs": bool(config_checks) and all(
            row["network_ids_present"] and row["associate_public_ip"]
            for row in config_checks
        ),
        "commands": bool(plan.get("jobs")) and all(
            "--builder" in command_for(job, output_root / "acceptance-overlay.toml")
            for job in plan["jobs"]
        ),
        "disk_free_gib": round(usage.free / (1024 ** 3), 2),
    }
    passed = bool(checks["credentials_named"] and checks["configs"] and checks["commands"])
    return {
        "schema": PREFLIGHT_SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "offline", "cloud_api_called": False, "passed": passed,
        "limitations": ["credential presence only; validity is checked on billed execution",
                        "cloud resource existence and quota are not checked offline"],
        "credentials": credentials, "config_checks": config_checks, "checks": checks,
    }


def build_plan(matrix: dict[str, Any], *, repeats: int, output_root: Path) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("at least one repeat is required")
    if matrix.get("schema") != MATRIX_SCHEMA:
        raise ValueError(f"unsupported matrix schema: {matrix.get('schema')!r}")
    if matrix.get("provider") != "tencentcloud":
        raise ValueError("acceptance matrix provider must be tencentcloud")
    jobs = []
    identities: set[tuple[str, int]] = set()
    for target in matrix.get("targets") or []:
        profile = str(target["profile"])
        level = int(target["level"])
        identity = (profile, level)
        if identity in identities:
            raise ValueError(f"duplicate acceptance target: {profile} L{level}")
        identities.add(identity)
        config = ROOT / "build-matrix" / f"{profile}-l{level}.toml"
        if not config.is_file():
            raise ValueError(f"missing acceptance config: {config.relative_to(ROOT)}")
        _validate_target_config(target, config)
        for repeat in range(1, repeats + 1):
            job_id = f"{profile}-l{level}-r{repeat}"
            job_root = output_root / job_id
            jobs.append({
                "job_id": job_id, "profile": profile, "level": level,
                "repeat": repeat, "config": str(config),
                "source_image_id": target["source_image_id"],
                "benchmark": target["benchmark"], "region": target["region"],
                "zone": target["zone"], "instance_type": target["instance_type"],
                "workdir": str(job_root / "work"), "state_dir": str(job_root / "state"),
                "result_file": str(job_root / "build-result.json"),
                "log_file": str(job_root / "build.log"),
                "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            })
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    changes = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return {"schema": SCHEMA, "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_revision": revision, "workspace_dirty": bool(changes.strip()),
            "acceptance_id": str(uuid.uuid4()),
            "provider": "tencentcloud", "billed": True, "repeats": repeats,
            "variance_evidence": repeats >= 3,
            "jobs": jobs, "job_count": len(jobs)}


def command_for(job: dict[str, Any], overlay: Path) -> list[str]:
    return [sys.executable, "-m", "ohbs_image", "--state-dir", job["state_dir"],
            "build", "--config", job["config"], "--overlay", str(overlay),
            "--workdir", job["workdir"], "--builder", "native", "--yes",
            "--result-file", job["result_file"], "--log-file", job["log_file"]]


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for job in plan.get("jobs") or []:
        result_path = Path(job["result_file"])
        record_path = Path(job["workdir"]) / "native" / "build-record.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) \
            if result_path.is_file() else {}
        record = json.loads(record_path.read_text(encoding="utf-8")) \
            if record_path.is_file() else {}
        if record:
            records.append(record)
        cleanup = record.get("cleanup") or {}
        row = {
            "job_id": job["job_id"], "profile": job["profile"], "level": job["level"],
            "repeat": job["repeat"], "exit_code": job.get("exit_code"),
            "result_status": result.get("status", "missing"),
            "record_status": record.get("status", "missing"), "score": result.get("score"),
            "clean_boot_passed": result.get("status") == "approved",
            "cleanup_status": cleanup.get("status", "missing"),
            "remaining_resources": cleanup.get("remaining_resources") or [],
            "result_file": str(result_path), "build_record": str(record_path),
        }
        row["passed"] = (
            row["result_status"] == "approved"
            and row["record_status"] == "completed"
            and row["cleanup_status"] == "completed"
            and not row["remaining_resources"]
        )
        rows.append(row)
        grouped.setdefault((str(job["profile"]), int(job["level"])), []).append(row)
    targets = []
    for (profile, level), values in sorted(grouped.items()):
        scores = [float(row["score"]) for row in values if row.get("score") is not None]
        targets.append({
            "profile": profile, "level": level, "runs": len(values),
            "passed_runs": sum(bool(row["passed"]) for row in values),
            "all_passed": bool(values) and all(row["passed"] for row in values),
            "score_min": min(scores) if scores else None,
            "score_max": max(scores) if scores else None,
            "score_variance": round(max(scores) - min(scores), 3) if scores else None,
        })
    return {
        "schema": SUMMARY_SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": rows, "targets": targets, "job_count": len(rows),
        "passed_jobs": sum(bool(row["passed"]) for row in rows),
        "complete": bool(rows) and all(row["passed"] for row in rows),
        "phase_benchmark": phase_benchmark(records),
    }


def write_summary(plan: dict[str, Any], output_root: Path) -> Path:
    target = output_root / "acceptance-summary.json"
    target.write_text(json.dumps(summarize_plan(plan), indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return target


def execute_plan(plan: dict[str, Any], output_root: Path, *, workers: int = 1,
                 network_config: Path | None = None) -> int:
    if workers < 1 or workers > 8:
        raise ValueError("workers must be between 1 and 8")
    output_root.mkdir(parents=True, exist_ok=True)
    overlay = output_root / "acceptance-overlay.toml"
    content = "[meta]\nverify_boot = true\ndelivery_report_required = true\n"
    if network_config is not None:
        network = load_config(network_config)["build"]
        content += "\n[build]\n"
        for name in ("vpc_id", "subnet_id", "security_group_id"):
            value = network.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"network config missing {name}")
            content += f"{name} = {json.dumps(value)}\n"
    overlay.write_text(content, encoding="utf-8")
    plan["overlay_sha256"] = hashlib.sha256(content.encode()).hexdigest()
    failures = 0
    lock = Lock()
    jobs = plan.get("jobs") or []
    if not jobs:
        return 0
    ingress_config = resolve(load_config_layered([Path(jobs[0]["config"]), overlay]))
    ingress_config.run_id = str(plan.get("acceptance_id") or uuid.uuid4())
    temporary_ingress = _create_temporary_ingress(ingress_config)

    def run_job(job: dict[str, Any]) -> int:
        Path(job["workdir"]).parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(command_for(job, overlay), cwd=ROOT, check=False)
        with lock:
            job["exit_code"] = completed.returncode
            job["status"] = "completed" if completed.returncode == 0 else "failed"
            (output_root / "acceptance-progress.json").write_text(
                json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            write_summary(plan, output_root)
        return completed.returncode

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ohbs-acceptance") as pool:
            futures = {pool.submit(run_job, job): job for job in jobs}
            for future in as_completed(futures):
                failures += future.result() != 0
    finally:
        _delete_temporary_ingress(ingress_config, temporary_ingress)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-root", default="build-matrix/next30-acceptance")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel billed builds (1-8; default 1)")
    parser.add_argument("--network-config", type=Path,
                        help="override only VPC, subnet and security group for this run")
    parser.add_argument("--summarize", action="store_true",
                        help="summarize existing artifacts without calling cloud APIs")
    parser.add_argument("--preflight", action="store_true",
                        help="run offline readiness checks without calling cloud APIs")
    parser.add_argument("--env-file", default=str(Path.home() / "wbenv"),
                        help="credential env file checked by variable name only")
    args = parser.parse_args(argv)
    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    output_root = Path(args.output_root).resolve()
    plan = build_plan(matrix, repeats=args.repeats, output_root=output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    plan_path = output_root / "acceptance-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"acceptance plan: {plan['job_count']} billed build jobs -> {plan_path}")
    if args.preflight:
        preflight = offline_preflight(plan, output_root=output_root,
                                      env_file=Path(args.env_file) if args.env_file else None)
        preflight_path = output_root / "acceptance-preflight.json"
        preflight_path.write_text(json.dumps(preflight, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
        print(f"offline preflight: {'passed' if preflight['passed'] else 'failed'} "
              f"-> {preflight_path}; no cloud API was called")
        return 0 if preflight["passed"] else 1
    if args.summarize:
        progress_path = output_root / "acceptance-progress.json"
        source = json.loads(progress_path.read_text(encoding="utf-8")) \
            if progress_path.is_file() else plan
        summary_path = write_summary(source, output_root)
        print(f"acceptance summary -> {summary_path}")
        return 0
    if not args.execute:
        print("plan only; no cloud API was called")
        return 0
    if not args.yes or os.environ.get("OHBS_ALLOW_BILLED_TESTS") != "1":
        print("refusing billed execution: require --execute --yes and OHBS_ALLOW_BILLED_TESTS=1",
              file=sys.stderr)
        return 2
    return execute_plan(plan, output_root, workers=args.workers,
                        network_config=args.network_config)


if __name__ == "__main__":
    raise SystemExit(main())
