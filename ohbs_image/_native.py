"""Compatibility facade for the cloud-neutral OHBS Native Engine.

New integrations should import :mod:`ohbs_image.native`.  These aliases keep
the original preview API stable while the implementation lives in the layered
native package.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import ohbs_image

from ._config import PackerResult, ResolvedConfig
from ._logging import ok
from ._native_contracts import NativeCommunicatorOps, NativeProviderOps
from .native.communicator.ssh import execute as _ssh
from .native.communicator.ssh import path_for_user as _path_for_user
from .native.communicator.ssh import upload as _scp
from .native.compiler import compile_hcl_provisioners, compile_workspace
from .native.executor import execute_build
from .native.providers.tencentcloud import copy_images as _copy_images
from .native.providers.tencentcloud import create_image as _create_image
from .native.providers.tencentcloud import instance_ip as _instance_ip
from .native.providers.tencentcloud import launch as _native_launch
from .native.providers.tencentcloud import teardown_keypair as _teardown_keypair
from .native.providers.tencentcloud import terminate as _terminate
from .native.providers.tencentcloud import wait_instance as _wait_instance_state
from .native.spec import ProvisionerSpec

NativeProvisioner = ProvisionerSpec


def parse_native_provisioners(hcl: str) -> list[NativeProvisioner]:
    """Compatibility wrapper around the BuildSpec compiler."""
    return list(compile_hcl_provisioners(hcl))


def run_native(workdir: Path, r: ResolvedConfig, image_name: str,
               *, timeout: int,
               emit: Callable[[str], None] | None = None,
               resume: bool = False,
               retain_on_failure: bool = False,
               provider: NativeProviderOps | None = None,
               communicator: NativeCommunicatorOps | None = None) -> PackerResult:
    """Compile and execute one native build through injected boundaries."""
    if r.family == "windows":
        return PackerResult(1, ["native builder currently supports Linux only"],
                            failure_category="unsupported")
    spec = compile_workspace(workdir, r, image_name)
    provider = provider or NativeProviderOps(
        setup_keypair=ohbs_image._probe_setup_keypair,
        launch=_native_launch, wait_instance=_wait_instance_state,
        instance_ip=_instance_ip, create_image=_create_image,
        copy_images=_copy_images, terminate=_terminate,
        teardown_keypair=_teardown_keypair,
        name=spec.target.provider,
    )
    communicator = communicator or NativeCommunicatorOps(
        ready=ohbs_image._probe_ssh_ready, upload=_scp, execute=_ssh,
        path_for_user=_path_for_user, name=spec.target.communicator)
    result = execute_build(
        spec, workdir, r, timeout=timeout, provider=provider,
        communicator=communicator, plan_digest=spec.sha256(),
        emit=emit, resume=resume, retain_on_failure=retain_on_failure)
    if result.exit_code == 0:
        image_ids = [line.split(": ", 1)[1] for line in result.stdout_lines
                     if line.startswith("Created image ID: ")]
        if image_ids:
            ok(f"Native image created: {image_ids[0]}")
    return result


__all__ = ["NativeProvisioner", "parse_native_provisioners", "run_native"]
