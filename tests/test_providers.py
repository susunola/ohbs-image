from __future__ import annotations

import argparse

import pytest

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

        def request(self, *args, **kwargs):
            return {}

        def discover(self, *args, **kwargs):
            return []

    with pytest.raises(ProviderCompatibilityError, match="required major"):
        verify_provider(OldProvider())


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
