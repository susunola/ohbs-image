"""Build-backend boundary shared by Packer and the native controller.

The native backend deliberately consumes the same rendered Packer workspace.
That keeps HCL as the public/debuggable build description while allowing the
controller implementation to be replaced incrementally.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ._config import PackerResult, ResolvedConfig
from ._logging import ConfigError
from .native.compiler import compile_workspace
from .native.providers.tencentcloud import SUPPORTED_PACKER_ARGS

BuilderName = Literal["packer", "native", "auto"]

NATIVE_PACKER_ARGS = SUPPORTED_PACKER_ARGS


def native_capability_issues(r: ResolvedConfig) -> list[str]:
    """Return deterministic reasons this configuration needs Packer."""
    issues: list[str] = []
    if r.family == "windows":
        issues.append("Windows/WinRM")
    if r.assume_role_arn:
        issues.append("cloud.assume_role_arn")
    unsupported = sorted(set(r.packer_extra) - NATIVE_PACKER_ARGS)
    if unsupported:
        issues.append("build.packer: " + ", ".join(unsupported))
    return issues


def select_builder(r: ResolvedConfig, requested: str) -> str:
    """Resolve the public auto controller to native or Packer."""
    if requested != "auto":
        return requested
    return "packer" if native_capability_issues(r) else "native"


@dataclass(frozen=True)
class NativePlan:
    """Stable, inspectable contract between rendering and native execution."""

    schema_version: int
    provider: str
    communicator: str
    profile: str
    image_name: str
    source_image_id: str
    region: str
    zone: str
    instance_type: str
    provisioners: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "communicator": self.communicator,
            "profile": self.profile,
            "image_name": self.image_name,
            "source_image_id": self.source_image_id,
            "region": self.region,
            "zone": self.zone,
            "instance_type": self.instance_type,
            "provisioners": list(self.provisioners),
        }


def _native_plan_steps(workdir: Path) -> tuple[str, ...]:
    """Return the exact generated provisioner order plus controller phases."""
    from .native.compiler import compile_hcl_provisioners

    hcl_path = Path(workdir) / "packer" / "main.pkr.hcl"
    parsed = compile_hcl_provisioners(hcl_path.read_text(encoding="utf-8"))
    steps: list[str] = ["connect"]
    for index, item in enumerate(parsed, 1):
        label = Path(item.source or item.script or item.remote_path).name
        if not label:
            label = f"inline-{index}"
        steps.append(f"{index:02d}:{item.kind}:{label}")
    steps.extend(("snapshot", "copy-images", "cleanup"))
    return tuple(steps)


def native_plan(workdir: Path, r: ResolvedConfig, image_name: str) -> NativePlan:
    """Create an executable native contract from the rendered HCL subset."""
    spec = compile_workspace(workdir, r, image_name)
    provisioners = spec.execution_steps()
    plan = NativePlan(
        schema_version=2,
        provider=spec.target.provider,
        communicator=spec.target.communicator,
        profile=spec.profile,
        image_name=image_name,
        source_image_id=spec.target.source_image_id,
        region=spec.target.region,
        zone=spec.target.zone,
        instance_type=spec.target.instance_type,
        provisioners=provisioners,
    )
    target = Path(workdir) / "native" / "plan.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.as_dict(), indent=2) + "\n", encoding="utf-8")
    spec_target = target.with_name("build-spec.json")
    spec_target.write_text(json.dumps(spec.as_dict(), indent=2) + "\n", encoding="utf-8")
    return plan


def validate_native(workdir: Path, r: ResolvedConfig, image_name: str) -> PackerResult:
    """Validate the native contract without invoking Packer or cloud write APIs."""
    lines: list[str] = []
    required = (
        "packer/main.pkr.hcl",
        "packer/auto.pkrvars.hcl",
        "packer/ansible-bundle.tar.gz",
        "packer/scripts/install-ansible.sh",
        "packer/scripts/ohbs-image-finalize.sh",
    )
    if r.family == "windows":
        return PackerResult(1, ["native builder currently supports Linux profiles only"],
                            failure_category="unsupported")
    for issue in native_capability_issues(r):
        lines.append("native builder capability unavailable: " + issue)
    for rel in required:
        if not (Path(workdir) / rel).is_file():
            lines.append(f"missing rendered input: {rel}")
    for tool in ("ssh", "scp", "ssh-keygen"):
        if shutil.which(tool) is None:
            lines.append(f"required native-builder tool not found: {tool}")
    try:
        plan = native_plan(workdir, r, image_name)
    except (OSError, ConfigError) as exc:
        lines.append(f"native execution plan is invalid: {exc}")
        plan = None
    if lines:
        return PackerResult(1, lines, failure_category="preflight")
    assert plan is not None
    lines.extend((
        f"Native plan: {workdir / 'native' / 'plan.json'}",
        f"Provider: {plan.provider}",
        f"Communicator: {plan.communicator}",
        f"Provisioners: {len(plan.provisioners)}",
    ))
    return PackerResult(0, lines)
