from __future__ import annotations

import argparse
import json

from ohbs_image._launch import _config_fingerprint, cmd_launch, cmd_run_resume


def _args(config, **overrides):
    values = {
        "config": str(config), "overlay": None, "workdir": ".build",
        "build": False, "yes": False, "offline": True, "output": "json",
        "quiet": False, "debug": False, "dry_run": False,
        "skip_if_unchanged": False, "log_file": None, "result_file": None,
        "timeout": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _config(path):
    path.write_text("""
[build]
profile = "tencentos3"
source_image_id = "img-source"
region = "ap-guangzhou"
zone = "ap-guangzhou-3"
instance_type = "S5.MEDIUM2"
vpc_id = "vpc-1"
subnet_id = "subnet-1"
security_group_id = "sg-1"
associate_public_ip = false
[image]
name_prefix = "test"
copy_regions = []
[ohbs]
level = 1
[cloud]
secret_id_env = "TENCENTCLOUD_SECRET_ID"
secret_key_env = "TENCENTCLOUD_SECRET_KEY"
""", encoding="utf-8")


def test_launch_requires_double_opt_in_for_build(tmp_path, caplog):
    config = tmp_path / "ohbs-image.toml"
    _config(config)
    assert cmd_launch(_args(config, build=True, yes=False)) == 2
    assert "requires --yes" in caplog.text


def test_launch_readiness_uses_one_run_id(tmp_path, monkeypatch, capsys):
    config = tmp_path / "ohbs-image.toml"
    _config(config)
    calls = []
    manifests = []
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._new_run_id", lambda: "run-123")
    monkeypatch.setattr("ohbs_image._launch.ohbs_image.main",
                        lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._write_run_manifest",
                        lambda r, **kwargs: manifests.append((r.run_id, kwargs)))

    assert cmd_launch(_args(config)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert result["run_id"] == "run-123"
    assert [stage["name"] for stage in result["stages"]] == ["doctor", "plan", "preflight"]
    assert "--run-id" in calls[1] and "run-123" in calls[1]
    assert all(run_id == "run-123" for run_id, _ in manifests)


def test_launch_stops_and_records_failed_stage(tmp_path, monkeypatch, capsys):
    config = tmp_path / "ohbs-image.toml"
    _config(config)
    returns = iter([0, 1])
    manifests = []
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._new_run_id", lambda: "run-456")
    monkeypatch.setattr("ohbs_image._launch.ohbs_image.main", lambda argv: next(returns))
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._write_run_manifest",
                        lambda r, **kwargs: manifests.append(kwargs))

    assert cmd_launch(_args(config)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["stages"][-1]["name"] == "plan"
    assert manifests[-1]["phase"] == "launch-plan"
    assert "next_action" in manifests[-1]


def test_launch_build_passes_same_run_id(tmp_path, monkeypatch, capsys):
    config = tmp_path / "ohbs-image.toml"
    _config(config)
    calls = []
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._new_run_id", lambda: "run-build")
    monkeypatch.setattr("ohbs_image._launch.ohbs_image.main",
                        lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._write_run_manifest",
                        lambda *args, **kwargs: None)

    assert cmd_launch(_args(config, build=True, yes=True, timeout=90)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    build = calls[-1]
    assert build[0] == "build"
    assert build[build.index("--run-id") + 1] == "run-build"
    assert build[build.index("--timeout") + 1] == "90"


def test_resume_skips_completed_stages_and_keeps_run_id(tmp_path, monkeypatch, capsys):
    config = tmp_path / "ohbs-image.toml"
    _config(config)
    checkpoint = {
        "version": 1, "workflow": "launch", "config": str(config.resolve()),
        "overlays": [], "config_fingerprint": _config_fingerprint(str(config), []),
        "workdir": str((tmp_path / ".build").resolve()),
        "completed_stages": ["doctor", "plan"],
    }
    calls = []
    manifest = {"run_id": "run-resume", "state": "FAILED", "checkpoint": checkpoint}
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._read_run_manifest",
                        lambda run_id: manifest)
    monkeypatch.setattr("ohbs_image._launch.verify_event_chain", lambda run_id: [])
    monkeypatch.setattr("ohbs_image._launch.ohbs_image.main",
                        lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._write_run_manifest",
                        lambda *args, **kwargs: None)

    args = argparse.Namespace(
        run_id="run-resume", build=False, yes=False, offline=True, output="json",
        quiet=False, debug=False, skip_if_unchanged=False, log_file=None,
        result_file=None, timeout=None)
    assert cmd_run_resume(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == "run-resume"
    assert [call[0] for call in calls] == ["preflight"]


def test_resume_refuses_changed_configuration(tmp_path, monkeypatch, caplog):
    config = tmp_path / "ohbs-image.toml"
    _config(config)
    checkpoint = {
        "workflow": "launch", "config": str(config), "overlays": [],
        "config_fingerprint": "stale", "workdir": str(tmp_path / ".build"),
        "completed_stages": ["doctor"],
    }
    monkeypatch.setattr("ohbs_image._launch.ohbs_image._read_run_manifest",
                        lambda run_id: {"state": "FAILED", "checkpoint": checkpoint})
    monkeypatch.setattr("ohbs_image._launch.verify_event_chain", lambda run_id: [])
    args = argparse.Namespace(run_id="run-changed", build=False, yes=False, offline=True,
                              output="text", quiet=False, debug=False,
                              skip_if_unchanged=False, log_file=None, result_file=None,
                              timeout=None)
    assert cmd_run_resume(args) == 1
    assert "Configuration changed" in caplog.text
