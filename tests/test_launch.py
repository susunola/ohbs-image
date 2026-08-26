from __future__ import annotations

import argparse
import json

from ohbs_image._launch import cmd_launch


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
