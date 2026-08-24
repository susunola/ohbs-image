from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from ._logging import fail, ok
from ._profiles import PROFILES

CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://ohbs-image.dev/config/v1",
    "title": "ohbs-image configuration",
    "type": "object",
    "required": ["build", "image", "ohbs", "cloud"],
    "properties": {
        "schema_version": {"const": 1},
        "build": {"type": "object", "required": ["profile", "region", "zone", "instance_type",
                    "source_image_id", "vpc_id", "subnet_id", "security_group_id",
                    "associate_public_ip"],
                  "properties": {"profile": {"enum": sorted(PROFILES)},
                                 "max_build_minutes": {"type": "integer", "minimum": 15,
                                                       "maximum": 1440}}},
        "image": {"type": "object"}, "ohbs": {"type": "object"},
        "cloud": {"type": "object"}, "meta": {"type": "object"},
        "state": {"type": "object", "properties": {
            "backend": {"enum": ["local", "cos"]}, "location": {"type": "string"}}},
    },
}

CONFIG_HELP = {
    "build.max_build_minutes": "Hard wall-clock limit for one Packer run; integer 15–1440, default 120.",
    "build.associate_public_ip": "Whether the temporary build CVM receives a public IP.",
    "ohbs.min_score": "Minimum post-reboot assessment score; 0 disables the score gate.",
    "meta.verify_boot": "Boot a clean probe from the produced image before release approval.",
    "state.backend": "Team evidence backend: local or cos.",
    "state.location": "Local directory or cos://bucket/prefix for team evidence.",
}


def cmd_config_schema(args: argparse.Namespace) -> int:
    payload = json.dumps(CONFIG_SCHEMA, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        ok(f"Configuration schema -> {args.output}")
    else:
        print(payload, end="")
    return 0


def cmd_config_explain(args: argparse.Namespace) -> int:
    text = CONFIG_HELP.get(args.key)
    if not text:
        fail(f"No explanation available for {args.key}")
        return 1
    print(f"{args.key}: {text}")
    return 0


def cmd_config_migrate(args: argparse.Namespace) -> int:
    path = Path(args.config)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"Could not read {path}: {exc}")
        return 1
    migrated = original
    if re.search(r"(?m)^\[cis\]\s*$", migrated) and not re.search(r"(?m)^\[ohbs\]\s*$", migrated):
        migrated = re.sub(r"(?m)^\[cis\]\s*$", "[ohbs]", migrated, count=1)
    if not re.search(r"(?m)^schema_version\s*=", migrated):
        migrated = "schema_version = 1\n\n" + migrated
    if migrated == original:
        ok("Configuration is already at schema version 1")
        return 0
    output = Path(args.output) if args.output else path
    if output == path and not args.apply:
        print(migrated, end="")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.migrate.tmp")
    temp.write_text(migrated, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, output)
    ok(f"Migrated configuration -> {output}")
    return 0
