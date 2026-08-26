from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from importlib import metadata
from typing import Any, Literal, Protocol, runtime_checkable

EXTENSION_API_VERSION = "1.0"
ExtensionKind = Literal["scanner", "signer", "notifier", "feed", "distributor"]
ENTRY_POINT_GROUPS: dict[ExtensionKind, str] = {
    "scanner": "ohbs_image.scanners", "signer": "ohbs_image.signers",
    "notifier": "ohbs_image.notifiers", "feed": "ohbs_image.feeds",
    "distributor": "ohbs_image.distributors",
}
REQUIRED_METHODS: dict[ExtensionKind, str] = {
    "scanner": "scan", "signer": "sign", "notifier": "notify",
    "feed": "poll", "distributor": "distribute",
}


@dataclass(frozen=True)
class ExtensionMetadata:
    name: str
    api_version: str
    kind: ExtensionKind
    capabilities: tuple[str, ...] = ()
    vendor: str = ""


@dataclass(frozen=True)
class ExtensionResult:
    status: Literal["ok", "failed", "partial"]
    facts: dict[str, Any]
    evidence: tuple[dict[str, Any], ...] = ()
    retryable: bool = False


@runtime_checkable
class Extension(Protocol):
    metadata: ExtensionMetadata

    def self_test(self) -> dict[str, Any]: ...


class ExtensionCompatibilityError(ValueError):
    pass


def verify_extension(value: object, *, run_self_test: bool = True) -> Extension:
    meta = getattr(value, "metadata", None)
    if not isinstance(meta, ExtensionMetadata):
        raise ExtensionCompatibilityError("extension must declare ExtensionMetadata")
    if not meta.name.strip() or any(character.isspace() for character in meta.name):
        raise ExtensionCompatibilityError("extension name must be non-empty and contain no whitespace")
    if meta.api_version.split(".", 1)[0] != EXTENSION_API_VERSION.split(".", 1)[0]:
        raise ExtensionCompatibilityError(
            f"extension {meta.name!r} uses API {meta.api_version!r}; "
            f"required major is {EXTENSION_API_VERSION.split('.', 1)[0]}"
        )
    method = REQUIRED_METHODS[meta.kind]
    if not callable(getattr(value, method, None)):
        raise ExtensionCompatibilityError(f"{meta.kind} extension must implement {method}()")
    self_test = getattr(value, "self_test", None)
    if not callable(self_test):
        raise ExtensionCompatibilityError("extension must implement self_test()")
    if run_self_test:
        result = self_test()
        if not isinstance(result, dict) or result.get("passed") is not True:
            raise ExtensionCompatibilityError(f"extension self-test failed: {result!r}")
        if not isinstance(result.get("checks"), list) or not result["checks"]:
            raise ExtensionCompatibilityError("extension self-test must report non-empty checks")
    return value  # type: ignore[return-value]


def load_extensions(kind: ExtensionKind | None = None) -> dict[str, Extension]:
    kinds = [kind] if kind else list(ENTRY_POINT_GROUPS)
    loaded: dict[str, Extension] = {}
    for selected in kinds:
        for entry in metadata.entry_points().select(group=ENTRY_POINT_GROUPS[selected]):
            candidate = entry.load()
            instance = candidate() if isinstance(candidate, type) else candidate
            extension = verify_extension(instance)
            name = extension.metadata.name
            if name in loaded:
                raise ExtensionCompatibilityError(f"duplicate extension name: {name}")
            loaded[name] = extension
    return loaded


def certification_document(extension: object) -> dict[str, Any]:
    verified = verify_extension(extension)
    result = verified.self_test()
    return {"schema": "https://ohbs-image.dev/extension-certification/v1",
            "compatible": True, "metadata": asdict(verified.metadata),
            "self_test": result}


def cmd_extension_list(args: argparse.Namespace) -> int:
    extensions = load_extensions(args.kind)
    rows = [asdict(extension.metadata) for extension in extensions.values()]
    print(json.dumps({"api_version": EXTENSION_API_VERSION, "extensions": rows},
                     indent=2, sort_keys=True))
    return 0


def cmd_extension_verify(args: argparse.Namespace) -> int:
    extensions = load_extensions(args.kind)
    if args.name not in extensions:
        raise ExtensionCompatibilityError(
            f"extension {args.name!r} not found; available: {', '.join(sorted(extensions))}"
        )
    print(json.dumps(certification_document(extensions[args.name]), indent=2, sort_keys=True))
    return 0
