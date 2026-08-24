from __future__ import annotations

import argparse
import json
from pathlib import Path

from ohbs_image import build_parser
from ohbs_image._onboarding import cmd_configure, cmd_doctor, cmd_plan
from ohbs_image._state import LocalStateBackend


def _configure_args(target: Path) -> argparse.Namespace:
    return argparse.Namespace(
        target=str(target), force=False, discover=False, profile="tencentos3",
        region="ap-guangzhou", zone="ap-guangzhou-3",
        source_image="img-abc12345", vpc="vpc-abc12345",
        subnet="subnet-abc12345", security_group="sg-abc12345",
        instance_type=None, level=1, public_ip=False,
    )


def test_configure_generates_resolvable_minimal_config(tmp_path):
    target = tmp_path / "ohbs-image.toml"
    assert cmd_configure(_configure_args(target)) == 0
    text = target.read_text(encoding="utf-8")
    assert 'profile = "tencentos3"' in text
    assert "associate_public_ip = false" in text


def test_configure_refuses_overwrite(tmp_path):
    target = tmp_path / "ohbs-image.toml"
    target.write_text("owned", encoding="utf-8")
    assert cmd_configure(_configure_args(target)) == 1
    assert target.read_text(encoding="utf-8") == "owned"


def test_plan_json_is_read_only(tmp_path, capsys):
    target = tmp_path / "ohbs-image.toml"
    assert cmd_configure(_configure_args(target)) == 0
    capsys.readouterr()
    args = argparse.Namespace(config=str(target), output="json")
    assert cmd_plan(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mutates_cloud"] is False
    assert doc["temporary_resources"][0]["count"] == 1
    assert doc["limits"]["maximum_minutes"] == 120


def test_doctor_missing_config_json(tmp_path, capsys):
    args = argparse.Namespace(config=str(tmp_path / "missing.toml"),
                              no_cloud=True, output="json")
    assert cmd_doctor(args) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ready"] is False
    assert any(c["id"] == "config" and c["status"] == "fail" for c in doc["checks"])


def test_local_state_backend_round_trip(tmp_path):
    state = tmp_path / "state"
    remote = tmp_path / "remote"
    restored = tmp_path / "restored"
    (state / "reports").mkdir(parents=True)
    (state / "lineage.jsonl").write_text('{"id":1}\n', encoding="utf-8")
    (state / "reports" / "one.json").write_text("{}", encoding="utf-8")
    backend = LocalStateBackend(remote)
    backend.push(state)
    backend.pull(restored)
    assert (restored / "lineage.jsonl").read_text(encoding="utf-8") == '{"id":1}\n'
    assert (restored / "reports" / "one.json").is_file()


def test_parser_exposes_first_wave_commands():
    parser = build_parser()
    assert parser.parse_args(["doctor", "--no-cloud"]).command == "doctor"
    assert parser.parse_args(["plan"]).command == "plan"
    state = parser.parse_args(["state", "sync", "push", "--backend", "cos",
                               "--location", "cos://bucket/state"])
    assert state.direction == "push"
