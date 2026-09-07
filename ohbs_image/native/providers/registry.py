"""Provider capability registry used by the cloud-neutral compiler/CLI."""
from __future__ import annotations

from dataclasses import dataclass

from ..._logging import ConfigError


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    communicators: tuple[str, ...]
    supports_spot: bool
    supports_image_copy: bool
    supports_resume: bool
    supports_assume_role: bool = False


_PROVIDERS: dict[str, ProviderCapabilities] = {}


def register_provider(capabilities: ProviderCapabilities, *aliases: str) -> None:
    for name in (capabilities.name, *aliases):
        normalized = name.strip().lower()
        if not normalized:
            raise ConfigError("native provider name cannot be empty")
        existing = _PROVIDERS.get(normalized)
        if existing is not None and existing != capabilities:
            raise ConfigError(f"native provider already registered: {normalized}")
        _PROVIDERS[normalized] = capabilities


def provider_capabilities(name: str) -> ProviderCapabilities:
    try:
        return _PROVIDERS[name.strip().lower()]
    except KeyError as exc:
        raise ConfigError(f"unknown native provider: {name}") from exc


def list_providers() -> tuple[ProviderCapabilities, ...]:
    return tuple(sorted(set(_PROVIDERS.values()), key=lambda item: item.name))
