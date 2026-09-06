"""Versioned, cloud-neutral build intermediate representation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ProvisionerSpec:
    kind: str
    source: str = ""
    destination: str = ""
    remote_path: str = ""
    script: str = ""
    inline: tuple[str, ...] = ()
    pause_before: float = 0
    start_retry_timeout: float = 0
    expect_disconnect: bool = False
    content_sha256: str = ""
    cacheable: bool = False

    @property
    def label(self) -> str:
        value = self.source or self.script or self.remote_path
        return value.rsplit("/", 1)[-1] if value else "inline"


@dataclass(frozen=True)
class CloudTarget:
    provider: str
    region: str
    zone: str
    source_image_id: str
    instance_type: str
    communicator: str = "ssh"


@dataclass(frozen=True)
class BuildSpec:
    schema_version: int
    profile: str
    image_name: str
    target: CloudTarget
    provisioners: tuple[ProvisionerSpec, ...]
    os_tag: str = ""
    benchmark: str = ""
    catalog_sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sha256(self) -> str:
        encoded = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def execution_steps(self) -> tuple[str, ...]:
        steps = ["connect"]
        for index, item in enumerate(self.provisioners, 1):
            label = item.label if item.label != "inline" else f"inline-{index}"
            steps.append(f"{index:02d}:{item.kind}:{label}")
        steps.extend(("snapshot", "copy-images", "cleanup"))
        return tuple(steps)
