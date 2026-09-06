"""Provider and communicator dependency contracts for the native core."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderOps:
    setup_keypair: Callable[..., tuple[str, str, str]]
    launch: Callable[..., str]
    wait_instance: Callable[..., dict[str, Any]]
    instance_ip: Callable[..., str]
    create_image: Callable[..., str]
    copy_images: Callable[..., list[str]]
    terminate: Callable[..., None]
    teardown_keypair: Callable[..., None]
    name: str = "provider"


@dataclass(frozen=True)
class CommunicatorOps:
    ready: Callable[..., bool]
    upload: Callable[..., list[str]]
    execute: Callable[..., list[str]]
    path_for_user: Callable[..., str]
    name: str = "communicator"
