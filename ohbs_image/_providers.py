from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from importlib import metadata
from typing import Any, Protocol, runtime_checkable

PROVIDER_API_VERSION = "1.0"
ENTRY_POINT_GROUP = "ohbs_image.providers"


@dataclass(frozen=True)
class ProviderCredentials:
    secret_id: str
    secret_key: str
    token: str | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    compute: bool = False
    images: bool = False
    networking: bool = False
    distribution: bool = False
    discovery: bool = False


@runtime_checkable
class Provider(Protocol):
    name: str
    api_version: str
    capabilities: ProviderCapabilities

    def request(self, service: str, action: str, version: str, region: str,
                params: dict[str, Any], credentials: ProviderCredentials,
                *, max_retries: int = 3) -> dict[str, Any]: ...

    def discover(self, resource: str, region: str, **filters: Any) -> list[dict[str, Any]]: ...

class ProviderCompatibilityError(ValueError):
    pass


def verify_provider(provider: object) -> Provider:
    name = getattr(provider, "name", "")
    version = getattr(provider, "api_version", "")
    capabilities = getattr(provider, "capabilities", None)
    if not isinstance(name, str) or not name.strip():
        raise ProviderCompatibilityError("provider name must be a non-empty string")
    if not isinstance(version, str) or version.split(".", 1)[0] != PROVIDER_API_VERSION.split(".", 1)[0]:
        raise ProviderCompatibilityError(
            f"provider {name!r} uses API {version!r}; required major is {PROVIDER_API_VERSION.split('.', 1)[0]}"
        )
    if not isinstance(capabilities, ProviderCapabilities):
        raise ProviderCompatibilityError(f"provider {name!r} must declare ProviderCapabilities")
    if not callable(getattr(provider, "request", None)) or not callable(getattr(provider, "discover", None)):
        raise ProviderCompatibilityError(f"provider {name!r} does not implement the provider protocol")
    maturity = getattr(provider, "maturity", "external-unverified")
    if maturity not in {"production", "contract-poc", "external-unverified"}:
        raise ProviderCompatibilityError(
            f"provider {name!r} declares an unsupported maturity {maturity!r}"
        )
    return provider  # type: ignore[return-value]


def _builtin_providers() -> dict[str, Provider]:
    from ._provider_aws_poc import AwsContractProvider
    from ._provider_tencentcloud import TencentCloudProvider

    providers = (TencentCloudProvider(), AwsContractProvider())
    return {provider.name: provider for provider in providers}


def load_providers(*, include_external: bool = True) -> dict[str, Provider]:
    providers = _builtin_providers()
    if not include_external:
        return providers
    entries = metadata.entry_points().select(group=ENTRY_POINT_GROUP)
    for entry in entries:
        candidate = entry.load()
        provider = candidate() if isinstance(candidate, type) else candidate
        verified = verify_provider(provider)
        if verified.name in providers:
            raise ProviderCompatibilityError(f"duplicate provider name: {verified.name}")
        providers[verified.name] = verified
    return providers


def get_provider(name: str) -> Provider:
    providers = load_providers()
    try:
        return providers[name]
    except KeyError as exc:
        raise ProviderCompatibilityError(
            f"unknown provider {name!r}; available: {', '.join(sorted(providers))}"
        ) from exc


def _provider_document(provider: Provider) -> dict[str, Any]:
    contract_test = getattr(provider, "contract_test", None)
    contract = contract_test() if callable(contract_test) else {
        "schema": "ohbs-image/provider-contract/v1",
        "offline": True,
        "checks": [
            {"name": "request-entrypoint", "passed": callable(provider.request)},
            {"name": "discovery-entrypoint", "passed": callable(provider.discover)},
        ],
        "limitations": ["provider does not expose optional contract_test() certification"],
    }
    checks = contract.get("checks")
    if not isinstance(checks, list) or not checks or not all(
        isinstance(check, dict) and check.get("passed") is True for check in checks
    ):
        raise ProviderCompatibilityError(f"provider {provider.name!r} failed its offline contract test")
    return {
        "name": provider.name,
        "api_version": provider.api_version,
        "compatible": True,
        "maturity": getattr(provider, "maturity", "external-unverified"),
        "production_ready": getattr(provider, "maturity", "external-unverified") == "production",
        "capabilities": asdict(provider.capabilities),
        "contract": contract,
    }


def cmd_provider_list(args: argparse.Namespace) -> int:
    documents = [_provider_document(provider) for provider in load_providers().values()]
    if args.output == "json":
        print(json.dumps({"schema": "ohbs-image/providers/v1", "providers": documents}, indent=2, sort_keys=True))
    else:
        for item in documents:
            enabled = ", ".join(name for name, value in item["capabilities"].items() if value)
            print(f"{item['name']}\tapi={item['api_version']}\tmaturity={item['maturity']}\t{enabled}")
    return 0


def cmd_provider_verify(args: argparse.Namespace) -> int:
    document = _provider_document(verify_provider(get_provider(args.name)))
    if args.output == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(
            f"provider {document['name']} is compatible with API {PROVIDER_API_VERSION} "
            f"({document['maturity']})"
        )
    return 0
