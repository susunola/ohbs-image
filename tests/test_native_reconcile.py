from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

from ohbs_image import build_parser
from ohbs_image._logging import ConfigError
from ohbs_image._native_reconcile import (
    _discover_expired_instances,
    _discover_instances,
    reconcile_native_resources,
    sweep_expired_resources,
)


def _runtime():
    return SimpleNamespace(
        region="ap-guangzhou", secret_id_env="SID", secret_key_env="SKEY",
        security_token_env="TOKEN", run_id="")


def test_native_reconcile_cli_is_read_only_by_default():
    args = build_parser().parse_args([
        "native", "reconcile", "run-123", "--config", "config.toml",
        "--output", "json"])
    assert args.run_id == "run-123"
    assert args.apply is False


def test_cloud_discovery_rechecks_all_ownership_tags():
    response = {"Response": {"InstanceSet": [
        {"InstanceId": "ins-owned", "InstanceState": "RUNNING", "Tags": [
            {"Key": "managed_by", "Value": "ohbs-image"},
            {"Key": "ephemeral", "Value": "true"},
            {"Key": "run_id", "Value": "run-123"}]},
        {"InstanceId": "ins-wrong-run", "InstanceState": "RUNNING", "Tags": [
            {"Key": "managed_by", "Value": "ohbs-image"},
            {"Key": "ephemeral", "Value": "true"},
            {"Key": "run_id", "Value": "someone-else"}]},
    ]}}
    with (
        mock.patch("ohbs_image._native_reconcile._creds",
                   return_value=("sid", "key", "")),
        mock.patch("ohbs_image._native_reconcile._tc3_api",
                   return_value=response) as api,
    ):
        found = _discover_instances(_runtime(), "run-123")
    assert [item["InstanceId"] for item in found] == ["ins-owned"]
    filters = api.call_args.args[4]["Filters"]
    assert {item["Name"] for item in filters} == {
        "tag:managed_by", "tag:ephemeral", "tag:run_id"}


def test_reconcile_check_writes_plan_without_mutating_cloud(tmp_path):
    instance = {"InstanceId": "ins-1", "InstanceState": "RUNNING"}
    with (
        mock.patch("ohbs_image._native_reconcile._discover_instances",
                   return_value=[instance]),
        mock.patch("ohbs_image._native_reconcile._local_key_candidate",
                   return_value=("skey-1", "")),
        mock.patch("ohbs_image._native_reconcile._validate_key",
                   return_value={"KeyId": "skey-1", "KeyName": "ohbs_123"}),
        mock.patch("ohbs_image._native_reconcile.terminate") as terminate,
        mock.patch("ohbs_image._native_reconcile.teardown_keypair") as teardown,
    ):
        result = reconcile_native_resources(
            _runtime(), tmp_path, "run-123", apply=False)
    assert result["mode"] == "check"
    assert [item["status"] for item in result["actions"]] == ["planned", "planned"]
    terminate.assert_not_called()
    teardown.assert_not_called()
    evidence = json.loads((tmp_path / "native/reconcile-run-123.json").read_text())
    assert evidence["resource_count"] == 2


def test_reconcile_apply_terminates_before_deleting_key(tmp_path):
    calls: list[str] = []
    with (
        mock.patch("ohbs_image._native_reconcile._discover_instances",
                   return_value=[{"InstanceId": "ins-1", "InstanceState": "RUNNING"}]),
        mock.patch("ohbs_image._native_reconcile._local_key_candidate",
                   return_value=("skey-1", "/tmp/private")),
        mock.patch("ohbs_image._native_reconcile._validate_key",
                   return_value={"KeyId": "skey-1", "KeyName": "ohbs_123"}),
        mock.patch("ohbs_image._native_reconcile.terminate",
                   side_effect=lambda *args: calls.append("instance")),
        mock.patch("ohbs_image._native_reconcile.teardown_keypair",
                   side_effect=lambda *args: calls.append("key")),
    ):
        result = reconcile_native_resources(
            _runtime(), tmp_path, "run-123", apply=True)
    assert calls == ["instance", "key"]
    assert result["failed"] == 0


def test_reconcile_never_deletes_key_when_instance_cleanup_fails(tmp_path):
    with (
        mock.patch("ohbs_image._native_reconcile._discover_instances",
                   return_value=[{"InstanceId": "ins-1", "InstanceState": "RUNNING"}]),
        mock.patch("ohbs_image._native_reconcile._local_key_candidate",
                   return_value=("skey-1", "")),
        mock.patch("ohbs_image._native_reconcile._validate_key",
                   return_value={"KeyId": "skey-1", "KeyName": "ohbs_123"}),
        mock.patch("ohbs_image._native_reconcile.terminate",
                   side_effect=RuntimeError("busy")),
        mock.patch("ohbs_image._native_reconcile.teardown_keypair") as teardown,
    ):
        result = reconcile_native_resources(
            _runtime(), tmp_path, "run-123", apply=True)
    assert result["failed"] == 1
    assert result["actions"][0]["error_type"] == "RuntimeError"
    teardown.assert_not_called()


def test_reconcile_rejects_path_like_run_id(tmp_path):
    import pytest
    with pytest.raises(ConfigError, match="safe bounded run ID"):
        reconcile_native_resources(_runtime(), tmp_path, "../../other", apply=False)


def test_expired_discovery_requires_all_ownership_and_lease_tags():
    def item(instance_id, tags):
        return {"InstanceId": instance_id, "Tags": [
            {"Key": key, "Value": value} for key, value in tags.items()]}
    response = {"Response": {"InstanceSet": [
        item("ins-expired", {"managed_by": "ohbs-image", "ephemeral": "true",
             "run_id": "run-1", "lease_expires_epoch": "99"}),
        item("ins-live", {"managed_by": "ohbs-image", "ephemeral": "true",
             "run_id": "run-2", "lease_expires_epoch": "101"}),
        item("ins-untagged", {"managed_by": "ohbs-image", "ephemeral": "true"}),
    ]}}
    with (mock.patch("ohbs_image._native_reconcile._creds", return_value=("s", "k", "")),
          mock.patch("ohbs_image._native_reconcile._tc3_api", return_value=response)):
        found = _discover_expired_instances(_runtime(), 100)
    assert [row["InstanceId"] for row in found] == ["ins-expired"]


def test_expired_sweep_is_read_only_by_default_and_writes_evidence(tmp_path):
    expired = {"InstanceId": "ins-1", "InstanceState": "RUNNING", "Tags": [
        {"Key": "managed_by", "Value": "ohbs-image"},
        {"Key": "ephemeral", "Value": "true"},
        {"Key": "run_id", "Value": "run-1"},
        {"Key": "lease_expires_epoch", "Value": "99"}]}
    with (mock.patch("ohbs_image._native_reconcile._discover_expired_instances",
                     return_value=[expired]),
          mock.patch("ohbs_image._native_reconcile.terminate") as terminate):
        result = sweep_expired_resources(_runtime(), tmp_path, now_epoch=100)
    terminate.assert_not_called()
    assert result["actions"][0]["status"] == "planned"
    assert result["resources"] == 1
    assert (tmp_path / "native/expired-lease-sweep-100.json").is_file()
