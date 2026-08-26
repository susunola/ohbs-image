from __future__ import annotations

import argparse

import pytest

from ohbs_image._provider_aws_poc import AwsContractProvider
from ohbs_image._provider_tencentcloud import TencentCloudProvider
from ohbs_image._providers import (
    PROVIDER_API_VERSION,
    ProviderCapabilities,
    ProviderCompatibilityError,
    ProviderCredentials,
    cmd_provider_list,
    cmd_provider_verify,
    load_providers,
    verify_provider,
)


def test_builtin_provider_declares_full_capabilities() -> None:
    provider = load_providers(include_external=False)["tencentcloud"]
    assert provider.api_version == PROVIDER_API_VERSION
    assert all(vars(provider.capabilities).values())


def test_incompatible_provider_is_rejected() -> None:
    class OldProvider:
        name = "old"
        api_version = "2.0"
        capabilities = ProviderCapabilities()
        maturity = "production"

        def request(self, *args, **kwargs):
            return {}

        def discover(self, *args, **kwargs):
            return []

        def contract_test(self):
            return {"checks": [{"passed": True}]}

    with pytest.raises(ProviderCompatibilityError, match="required major"):
        verify_provider(OldProvider())


def test_api_v1_provider_without_optional_certification_remains_compatible() -> None:
    class ExistingProvider:
        name = "existing"
        api_version = "1.0"
        capabilities = ProviderCapabilities(images=True)

        def request(self, *args, **kwargs):
            return {}

        def discover(self, *args, **kwargs):
            return []

    assert verify_provider(ExistingProvider()).name == "existing"


def test_tencent_adapter_delegates_credentials(monkeypatch) -> None:
    seen = {}

    def fake_api(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {"Response": {"RequestId": "r-1"}}

    monkeypatch.setattr("ohbs_image._tc_cloud._tc3_api", fake_api)
    result = TencentCloudProvider().request(
        "cvm", "DescribeImages", "2017-03-12", "ap-test", {},
        ProviderCredentials("sid", "skey", "token"), max_retries=1,
    )
    assert result["Response"]["RequestId"] == "r-1"
    assert seen["args"][5:] == ("sid", "skey", "token")
    assert seen["kwargs"] == {"max_retries": 1}


def test_provider_commands_emit_json(capsys) -> None:
    assert cmd_provider_list(argparse.Namespace(output="json")) == 0
    assert '"tencentcloud"' in capsys.readouterr().out
    assert cmd_provider_verify(argparse.Namespace(name="tencentcloud", output="json")) == 0
    assert '"compatible": true' in capsys.readouterr().out


def test_aws_contract_poc_is_offline_and_explicitly_not_production() -> None:
    provider = AwsContractProvider()
    result = provider.request(
        "ec2", "RunInstances", "2016-11-15", "us-east-1", {"ImageId": "ami-1"},
        ProviderCredentials("secret-must-not-appear", "secret-must-not-appear"),
    )
    assert result["ContractOnly"] is True
    assert result["NetworkSent"] is False
    assert "secret-must-not-appear" not in repr(result)
    contract = provider.contract_test()
    assert contract["production_ready"] is False
    assert all(check["passed"] for check in contract["checks"])


def test_aws_contract_discovery_uses_defensive_fixture_copies() -> None:
    provider = AwsContractProvider()
    fixture = {"id": "ami-1", "region": "us-east-1", "state": "available"}
    result = provider.discover("images", "us-east-1", fixtures=[fixture], state="available")
    result[0]["state"] = "mutated"
    assert fixture["state"] == "available"


def test_builtins_report_maturity_and_pass_contracts(capsys) -> None:
    assert cmd_provider_list(argparse.Namespace(output="json")) == 0
    document = capsys.readouterr().out
    assert '"aws-contract-poc"' in document
    assert '"production_ready": false' in document
    assert '"maturity": "production"' in document
