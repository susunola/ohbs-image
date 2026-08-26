from __future__ import annotations

from typing import Any

from ._providers import PROVIDER_API_VERSION, ProviderCapabilities, ProviderCredentials


class TencentCloudProvider:
    """Built-in Tencent Cloud adapter for the stable provider API."""

    name = "tencentcloud"
    api_version = PROVIDER_API_VERSION
    capabilities = ProviderCapabilities(
        compute=True,
        images=True,
        networking=True,
        distribution=True,
        discovery=True,
    )

    def request(self, service: str, action: str, version: str, region: str,
                params: dict[str, Any], credentials: ProviderCredentials,
                *, max_retries: int = 3) -> dict[str, Any]:
        from ._tc_cloud import _tc3_api

        return _tc3_api(service, action, version, region, params,
                        credentials.secret_id, credentials.secret_key,
                        credentials.token, max_retries=max_retries)

    def discover(self, resource: str, region: str, **filters: Any) -> list[dict[str, Any]]:
        from ._discover import discover_resources

        result = discover_resources(resource, region, **filters)
        if not isinstance(result, list):
            raise TypeError("provider discovery must return a list")
        return result
