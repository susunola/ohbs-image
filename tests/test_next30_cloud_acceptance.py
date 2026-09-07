from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    path = Path(__file__).parents[1] / "scripts/run_next30_cloud_acceptance.py"
    spec = importlib.util.spec_from_file_location("next30_acceptance", path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_acceptance_plan_covers_12_targets_three_times(tmp_path: Path) -> None:
    acceptance = module()
    matrix = json.loads((Path(__file__).with_name(
        "golden-matrix-tencent-linux.json")).read_text(encoding="utf-8"))
    plan = acceptance.build_plan(matrix, repeats=3, output_root=tmp_path)
    assert plan["billed"] is True
    assert plan["job_count"] == 36
    assert plan["variance_evidence"] is True
    assert len({(job["profile"], job["level"]) for job in plan["jobs"]}) == 12
    assert all(job["source_image_id"].startswith("img-") for job in plan["jobs"])


def test_acceptance_plan_supports_single_pass_matrix(tmp_path: Path) -> None:
    acceptance = module()
    matrix = json.loads((Path(__file__).with_name(
        "golden-matrix-tencent-linux.json")).read_text(encoding="utf-8"))
    plan = acceptance.build_plan(matrix, repeats=1, output_root=tmp_path)
    assert plan["job_count"] == 12
    assert plan["variance_evidence"] is False


def test_acceptance_command_forces_native_and_machine_evidence(tmp_path: Path) -> None:
    acceptance = module()
    job = {"state_dir": str(tmp_path / "state"), "config": "config.toml",
           "workdir": str(tmp_path / "work"), "result_file": str(tmp_path / "result.json"),
           "log_file": str(tmp_path / "build.log")}
    command = acceptance.command_for(job, tmp_path / "overlay.toml")
    assert command[command.index("--builder") + 1] == "native"
    assert "--yes" in command
    assert "--temporary-ingress" not in command
    assert "--result-file" in command and "--log-file" in command


def test_acceptance_plan_rejects_config_drift(tmp_path: Path, monkeypatch) -> None:
    acceptance = module()
    root = tmp_path / "repo"
    config_dir = root / "build-matrix"
    config_dir.mkdir(parents=True)
    (config_dir / "rocky9-l1.toml").write_text(
        """[build]
profile = "rocky9"
region = "ap-guangzhou"
zone = "ap-guangzhou-6"
instance_type = "S5.MEDIUM2"
source_image_id = "img-wrong"
[ohbs]
level = 1
[meta]
benchmark = "CIS-v2.0.0"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(acceptance, "ROOT", root)
    matrix = {
        "schema": acceptance.MATRIX_SCHEMA,
        "provider": "tencentcloud",
        "targets": [{
            "profile": "rocky9", "level": 1, "source_image_id": "img-correct",
            "benchmark": "CIS-v2.0.0", "region": "ap-guangzhou",
            "zone": "ap-guangzhou-6", "instance_type": "S5.MEDIUM2",
        }],
    }
    with pytest.raises(ValueError, match="source_image_id"):
        acceptance.build_plan(matrix, repeats=3, output_root=tmp_path / "out")


def test_acceptance_plan_rejects_duplicate_target(tmp_path: Path) -> None:
    acceptance = module()
    matrix = json.loads((Path(__file__).with_name(
        "golden-matrix-tencent-linux.json")).read_text(encoding="utf-8"))
    matrix["targets"].append(dict(matrix["targets"][0]))
    with pytest.raises(ValueError, match="duplicate acceptance target"):
        acceptance.build_plan(matrix, repeats=3, output_root=tmp_path)


def test_acceptance_summary_requires_result_record_and_cleanup(tmp_path: Path) -> None:
    acceptance = module()
    workdir = tmp_path / "work"
    result_file = tmp_path / "result.json"
    record_file = workdir / "native" / "build-record.json"
    record_file.parent.mkdir(parents=True)
    result_file.write_text(json.dumps({"status": "approved", "score": 99.5}), encoding="utf-8")
    record_file.write_text(json.dumps({
        "status": "completed", "cleanup": {"status": "completed", "remaining_resources": []},
        "phase_duration_seconds": {"provision": 10.0, "total": 20.0},
    }), encoding="utf-8")
    plan = {"jobs": [{
        "job_id": "rocky9-l1-r1", "profile": "rocky9", "level": 1, "repeat": 1,
        "result_file": str(result_file), "workdir": str(workdir), "exit_code": 0,
    }]}
    summary = acceptance.summarize_plan(plan)
    assert summary["complete"] is True
    assert summary["passed_jobs"] == 1
    assert summary["targets"][0]["score_variance"] == 0.0
    assert summary["phase_benchmark"]["phases"]["total"]["p95_seconds"] == 20.0


def test_acceptance_summary_exposes_missing_evidence_and_leaks(tmp_path: Path) -> None:
    acceptance = module()
    workdir = tmp_path / "work"
    record_file = workdir / "native" / "build-record.json"
    record_file.parent.mkdir(parents=True)
    record_file.write_text(json.dumps({
        "status": "failed", "cleanup": {"status": "failed",
        "remaining_resources": [{"kind": "instance", "id": "ins-leaked"}]},
    }), encoding="utf-8")
    summary = acceptance.summarize_plan({"jobs": [{
        "job_id": "rhel10-l2-r1", "profile": "rhel10", "level": 2, "repeat": 1,
        "result_file": str(tmp_path / "missing.json"), "workdir": str(workdir),
    }]})
    assert summary["complete"] is False
    assert summary["jobs"][0]["result_status"] == "missing"
    assert summary["jobs"][0]["remaining_resources"][0]["id"] == "ins-leaked"


def test_offline_preflight_reads_only_credential_names(tmp_path: Path, monkeypatch) -> None:
    acceptance = module()
    root = tmp_path / "repo"
    config = root / "build-matrix" / "rocky9-l1.toml"
    config.parent.mkdir(parents=True)
    config.write_text("""[build]
vpc_id = "vpc-test"
subnet_id = "subnet-test"
security_group_id = "sg-test"
associate_public_ip = true
""", encoding="utf-8")
    env_file = tmp_path / "wbenv"
    secret_value = "must-not-appear-in-report"
    env_file.write_text(
        f"export TENCENTCLOUD_SECRET_ID={secret_value}\n"
        f"TENCENTCLOUD_SECRET_KEY={secret_value}\n", encoding="utf-8")
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    plan = {"jobs": [{
        "job_id": "rocky9-l1-r1", "config": str(config), "state_dir": "state",
        "workdir": "work", "result_file": "result", "log_file": "log",
    }]}
    report = acceptance.offline_preflight(plan, output_root=tmp_path, env_file=env_file)
    assert report["passed"] is True
    assert report["cloud_api_called"] is False
    assert secret_value not in json.dumps(report)


def test_offline_preflight_fails_when_network_id_is_missing(tmp_path: Path,
                                                            monkeypatch) -> None:
    acceptance = module()
    config = tmp_path / "config.toml"
    config.write_text("""[build]
vpc_id = "vpc-test"
subnet_id = ""
security_group_id = "sg-test"
associate_public_ip = true
""", encoding="utf-8")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "present")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "present")
    plan = {"jobs": [{
        "job_id": "job", "config": str(config), "state_dir": "state", "workdir": "work",
        "result_file": "result", "log_file": "log",
    }]}
    report = acceptance.offline_preflight(plan, output_root=tmp_path)
    assert report["passed"] is False
    assert report["config_checks"][0]["missing_network_ids"] == ["subnet_id"]


def test_execute_plan_rejects_unsafe_worker_count(tmp_path: Path) -> None:
    acceptance = module()
    with pytest.raises(ValueError, match="workers must be between"):
        acceptance.execute_plan({"jobs": []}, tmp_path, workers=9)


def test_execute_plan_owns_one_shared_ingress_rule(tmp_path: Path, monkeypatch) -> None:
    acceptance = module()
    events = []
    monkeypatch.setattr(acceptance, "load_config_layered", lambda paths: {"paths": paths})
    resolved = SimpleNamespace(run_id="")
    monkeypatch.setattr(acceptance, "resolve", lambda data: resolved)
    monkeypatch.setattr(acceptance, "_create_temporary_ingress",
                        lambda value: events.append(("create", value)) or {"rule": "owned"})
    monkeypatch.setattr(acceptance, "_delete_temporary_ingress",
                        lambda value, rule: events.append(("delete", value, rule)))
    monkeypatch.setattr(acceptance.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0))
    job = {
        "job_id": "job", "profile": "rocky9", "level": 1, "repeat": 1,
        "config": str(tmp_path / "config.toml"), "state_dir": str(tmp_path / "state"),
        "workdir": str(tmp_path / "work"), "result_file": str(tmp_path / "result.json"),
        "log_file": str(tmp_path / "build.log"),
    }
    assert acceptance.execute_plan({"acceptance_id": "acceptance-test", "jobs": [job]},
                                   tmp_path, workers=1) == 0
    assert resolved.run_id == "acceptance-test"
    assert events == [("create", resolved),
                      ("delete", resolved, {"rule": "owned"})]
