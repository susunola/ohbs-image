"""Compile the supported Packer-HCL surface into the native BuildSpec IR."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .._logging import ConfigError
from .spec import BuildSpec, CloudTarget, ProvisionerSpec


def inline_script_content(commands: tuple[str, ...]) -> str:
    """Return the exact UTF-8 payload uploaded for an inline provisioner."""
    return "#!/usr/bin/env bash\n" + "\n".join(commands) + "\n"


def _balanced(text: str, start: int, opener: str, closer: str) -> tuple[str, int]:
    depth = 0
    quoted = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start + 1:pos], pos + 1
    raise ConfigError(f"unterminated HCL {opener}{closer} block")


def _hcl_string(body: str, key: str) -> str:
    match = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*("(?:\\.|[^"\\])*")', body)
    return str(json.loads(match.group(1))) if match else ""


def _duration(value: str) -> float:
    if not value:
        return 0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", value)
    if not match:
        raise ConfigError(f"unsupported native provisioner duration: {value}")
    return float(match.group(1)) * {"ms": 0.001, "s": 1, "m": 60}[match.group(2)]


def compile_hcl_provisioners(hcl: str) -> tuple[ProvisionerSpec, ...]:
    parsed: list[ProvisionerSpec] = []
    pattern = re.compile(r'provisioner\s+"(shell|file)"\s*\{')
    cursor = 0
    while match := pattern.search(hcl, cursor):
        body, cursor = _balanced(hcl, match.end() - 1, "{", "}")
        inline: tuple[str, ...] = ()
        inline_match = re.search(r"\binline\s*=\s*\[", body)
        if inline_match:
            raw, _ = _balanced(body, inline_match.end() - 1, "[", "]")
            try:
                inline = tuple(str(item) for item in json.loads(f"[{raw}]"))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ConfigError(f"unsupported generated HCL inline array: {exc}") from exc
        content_sha256 = ""
        if inline:
            content_sha256 = hashlib.sha256(
                inline_script_content(inline).encode()).hexdigest()
        parsed.append(ProvisionerSpec(
            kind=match.group(1), source=_hcl_string(body, "source"),
            destination=_hcl_string(body, "destination"),
            remote_path=_hcl_string(body, "remote_path"),
            script=_hcl_string(body, "script"), inline=inline,
            pause_before=_duration(_hcl_string(body, "pause_before")),
            start_retry_timeout=_duration(_hcl_string(body, "start_retry_timeout")),
            expect_disconnect=bool(re.search(
                r"(?m)^\s*expect_disconnect\s*=\s*true\s*$", body)),
            content_sha256=content_sha256,
        ))
    if not parsed:
        raise ConfigError("generated HCL contains no supported shell/file provisioners")
    return tuple(parsed)


def compile_workspace(workdir: Path, resolved: Any, image_name: str) -> BuildSpec:
    hcl = (Path(workdir) / "packer" / "main.pkr.hcl").read_text(encoding="utf-8")
    provisioners = list(compile_hcl_provisioners(hcl))
    for index, item in enumerate(provisioners):
        relative = item.source or item.script
        if not relative:
            continue
        path = Path(workdir) / relative
        if path.is_file():
            provisioners[index] = replace(
                item, content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                # Only OHBS's generated, non-secret role archive persists in
                # the image cache. Arbitrary file/script provisioners remain
                # one-shot even when they happen to have the same digest.
                cacheable=Path(relative).as_posix() ==
                "packer/ansible-bundle.tar.gz")
    target = CloudTarget(
        provider="tencentcloud-cvm", region=resolved.region, zone=resolved.zone,
        source_image_id=resolved.source_image_id, instance_type=resolved.instance_type,
        communicator="winrm" if resolved.family == "windows" else "ssh",
    )
    catalog = (Path(workdir) / "ansible" / "roles" / resolved.role_dir
               / "files" / "rules.json")
    catalog_sha256 = hashlib.sha256(catalog.read_bytes()).hexdigest() \
        if catalog.is_file() else ""
    return BuildSpec(
        schema_version=3, profile=resolved.profile_name, image_name=image_name,
        target=target, provisioners=tuple(provisioners),
        os_tag=str(resolved.image_os_tag), benchmark=str(resolved.image_benchmark),
        catalog_sha256=catalog_sha256)
