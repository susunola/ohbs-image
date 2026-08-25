from __future__ import annotations

import argparse
import json
from pathlib import Path

from ohbs_image import build_parser
from ohbs_image._onboarding import cmd_configure, cmd_doctor, cmd_plan
from ohbs_image._state import CosStateBackend, LocalStateBackend


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
                              no_cloud=True, output="json",
                              only="all", offline=False, report_path=None)
    # EXIT_CONFIG=2 — the configuration could not be resolved.
    assert cmd_doctor(args) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["ready"] is False
    assert doc["diagnostics"]["exit_code"] == 2
    assert any(c["id"] == "config" and c["status"] == "fail" for c in doc["checks"])


def test_doctor_cloud_contract_and_relationships(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ohbs-image.toml"
    assert cmd_configure(_configure_args(target)) == 0
    capsys.readouterr()
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    monkeypatch.setattr("ohbs_image._creds", lambda *a: ("sid", "key", None))

    def fake_api(service, action, version, region, params, sid, key, token):
        if action == "DescribeImages":
            return {"Response": {"ImageSet": [{"ImageId": "img-abc12345"}]}}
        if action == "DescribeSubnets":
            return {"Response": {"SubnetSet": [{"SubnetId": "subnet-abc12345",
                                                  "VpcId": "vpc-abc12345",
                                                  "Zone": "ap-guangzhou-3"}]}}
        if action == "DescribeSecurityGroups":
            return {"Response": {"SecurityGroupSet": [{"SecurityGroupId": "sg-abc12345"}]}}
        raise AssertionError(action)

    monkeypatch.setattr("ohbs_image._tc3_api", fake_api)
    args = argparse.Namespace(config=str(target), no_cloud=False, output="json",
                              only="all", offline=False, report_path=None)
    cmd_doctor(args)
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"schema", "ready", "checks", "diagnostics"}
    assert doc["diagnostics"]["redacted"] is True
    assert doc["diagnostics"]["exit_code"] in (0, 1)
    statuses = {check["id"]: check["status"] for check in doc["checks"]}
    assert statuses["cloud.source_image"] == "pass"
    assert statuses["cloud.subnet_vpc"] == "pass"
    assert statuses["cloud.subnet_zone"] == "pass"
    assert statuses["cloud.security_group"] == "pass"


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


def test_cos_backend_uses_explicit_temp_config(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("ohbs_image._state.shutil.which", lambda name: "/usr/bin/coscli")
    monkeypatch.setenv("OHBS_IMAGE_COSCLI_CONFIG", str(tmp_path / "cos.yaml"))
    monkeypatch.setattr("ohbs_image._state.subprocess.run",
                        lambda command, timeout: calls.append(command) or
                        argparse.Namespace(returncode=0))
    source = tmp_path / "state"
    source.mkdir()
    CosStateBackend("cos://bucket/prefix").push(source)
    assert calls == [["coscli", "sync", "--recursive", "-c", str(tmp_path / "cos.yaml"),
                      str(source.resolve()) + "/", "cos://bucket/prefix/"]]


def test_parser_exposes_first_wave_commands():
    parser = build_parser()
    assert parser.parse_args(["doctor", "--no-cloud"]).command == "doctor"
    assert parser.parse_args(["plan"]).command == "plan"
    state = parser.parse_args(["state", "sync", "push", "--backend", "cos",
                               "--location", "cos://bucket/state"])
    assert state.direction == "push"


def test_plan_v1_contract_shape(tmp_path, capsys):
    target = tmp_path / "ohbs-image.toml"
    assert cmd_configure(_configure_args(target)) == 0
    capsys.readouterr()
    assert cmd_plan(argparse.Namespace(config=str(target), output="json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert list(doc) == ["schema", "mutates_cloud", "profile", "family", "cis_level",
                         "placement", "source_image_id", "temporary_resources", "outputs",
                         "gates", "distribution", "limits", "cost"]
    assert doc["schema"] == "https://ohbs-image.dev/plan/v1"
