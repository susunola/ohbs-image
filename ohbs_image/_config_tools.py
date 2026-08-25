from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from ._config import load_config, load_config_layered, resolve
from ._logging import ConfigError, fail, ok, warn
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

# Roadmap E — every configurable key, in display order, with the dotted key
# used by `config explain` / `config get`. Values are read by resolve().
CONFIG_HELP: dict[str, str] = {
    "build.profile": "CIS hardening profile (e.g. tencentos3, win2022); must be a known profile name.",
    "build.region": "Tencent Cloud region for the build (e.g. ap-guangzhou).",
    "build.zone": "Availability zone inside the region (e.g. ap-guangzhou-3).",
    "build.instance_type": "Build CVM specifier, full form required (e.g. S5.MEDIUM2).",
    "build.source_image_id": "Source image ID the golden image is hardened from (img-...).",
    "build.vpc_id": "VPC the temporary build CVM is launched into (vpc-...).",
    "build.subnet_id": "Subnet for the build CVM (subnet-...); must match zone.",
    "build.security_group_id": "Security group for the build CVM (sg-...).",
    "build.associate_public_ip": "Whether the temporary build CVM gets a public IP.",
    "build.instance_name": "Explicit name for the build CVM ('' = plugin auto-name).",
    "build.spot": "Use a spot instance for the build VM (cheaper, may be interrupted).",
    "build.max_build_minutes": "Hard wall-clock limit for one Packer run; integer 15-1440, default 120.",
    "build.packer": "Arbitrary extra tencentcloud-cvm builder args, passthrough to HCL.",
    "image.name_prefix": "Prefix of the produced image name (final name = prefix-timestamp).",
    "image.name": "Fixed image name; overrides name_prefix auto-naming when set.",
    "image.copy_regions": "Regions the image is copied to after the build (list of region codes).",
    "image.share_accounts": "UINs the image is shared with after the build.",
    "image.share_org_units": "Org units for image sharing (parsed; no org-unit share API yet).",
    "ohbs.level": "CIS level to enforce: 1 or 2.",
    "ohbs.min_score": "Minimum post-reboot assessment score; 0 disables the score gate.",
    "ohbs.allow_disruptive": "Apply disruptive remediations (reboot/firewall changes) mid-build.",
    "ohbs.allow_scoped_approval": "Explicitly permit a rule-subset (rules_include) image.",
    "ohbs.rules_include": "Only audit/apply these rule IDs (empty = all rules).",
    "ohbs.rules_exclude": "Skip these rule IDs; wins over rules_include.",
    "ohbs.overrides": "Per-rule parameter deep-merge: rule_id -> {param: value}.",
    "cloud.secret_id_env": "Env var holding the Tencent Cloud SecretId.",
    "cloud.secret_key_env": "Env var holding the Tencent Cloud SecretKey.",
    "cloud.security_token_env": "Env var holding the STS session token (default TENCENTCLOUD_SECURITY_TOKEN).",
    "cloud.assume_role_arn": "CAM role ARN to assume for the build ('' = off).",
    "cloud.assume_role_session": "Session name used when assuming the role (default ohbs-image).",
    "cloud.assume_role_duration": "Role session duration in seconds, 0-43200 (default 7200).",
    "meta.os_tag": "OS tag attached to the image (e.g. tencentos3, windows-2022).",
    "meta.benchmark": "CIS benchmark edition this profile targets.",
    "meta.smoke_test": "Run instance-level smoke checks before snapshotting the image.",
    "meta.cve_scan": "Run a trivy vulnerability scan gate before snapshot (default false).",
    "meta.sbom": "Emit an SBOM into the image and provenance (default false).",
    "meta.delivery_report_required": "Fail release if the HTML delivery report cannot be written.",
    "meta.verify_boot": "Boot a clean probe from the produced image and re-audit before success.",
    "meta.test_components": "User-defined test scripts run on the build instance before snapshot.",
    "meta.ssh_debug_password": "Optional SSH password for debugging the build instance.",
    "state.backend": "Team evidence backend: local or cos.",
    "state.location": "Local directory or cos://bucket/prefix for team evidence.",
    "notify.webhook": "WeCom group-robot webhook URL for build notifications ('' = off).",
    "notify.on": "When to notify: always, success, or failure (default failure).",
    "notify.deploy_webhook": "POST image metadata on build success ('' = off).",
    "sign.gpg_key": "GPG key id/fingerprint for SLSA provenance signing ('' = off).",
    "attestation.required": "Signed provenance is required before release approval.",
}

# Dotted config key -> ResolvedConfig attribute (for `config get`).
_RESOLVED_KEY_MAP: dict[str, str] = {
    "build.profile": "profile_name",
    "build.region": "region",
    "build.zone": "zone",
    "build.instance_type": "instance_type",
    "build.source_image_id": "source_image_id",
    "build.vpc_id": "vpc_id",
    "build.subnet_id": "subnet_id",
    "build.security_group_id": "security_group_id",
    "build.associate_public_ip": "associate_public_ip",
    "build.instance_name": "instance_name",
    "build.spot": "spot",
    "build.max_build_minutes": "max_build_minutes",
    "image.name_prefix": "image_name_prefix",
    "image.name": "image_name_override",
    "image.copy_regions": "image_copy_regions",
    "image.share_accounts": "image_share_accounts",
    "image.share_org_units": "image_share_org_units",
    "ohbs.level": "level",
    "ohbs.min_score": "min_score",
    "ohbs.allow_disruptive": "allow_disruptive",
    "ohbs.allow_scoped_approval": "allow_scoped_approval",
    "ohbs.rules_include": "rules_include",
    "ohbs.rules_exclude": "rules_exclude",
    "ohbs.overrides": "rules_overrides",
    "cloud.secret_id_env": "secret_id_env",
    "cloud.secret_key_env": "secret_key_env",
    "cloud.security_token_env": "security_token_env",
    "cloud.assume_role_arn": "assume_role_arn",
    "cloud.assume_role_session": "assume_role_session",
    "cloud.assume_role_duration": "assume_role_duration",
    "meta.os_tag": "image_os_tag",
    "meta.benchmark": "image_benchmark",
    "meta.smoke_test": "smoke_test",
    "meta.cve_scan": "cve_scan",
    "meta.sbom": "sbom",
    "meta.delivery_report_required": "delivery_report_required",
    "meta.verify_boot": "verify_boot",
    "meta.test_components": "test_components",
    "meta.ssh_debug_password": "ssh_debug_password",
    "notify.webhook": "notify_webhook",
    "notify.on": "notify_on",
    "notify.deploy_webhook": "deploy_webhook",
    "sign.gpg_key": "sign_key",
    "attestation.required": "attestation_required",
}

_RAW_DEFAULTS: dict[str, Any] = {
    "state.backend": "local",
    "state.location": "~/.ohbs-image",
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
    if getattr(args, "all", False):
        current = None
        for key, text in CONFIG_HELP.items():
            section = key.split(".", 1)[0]
            if section != current:
                print(f"\n[{section}]")
                current = section
            print(f"  {key}: {text}")
        return 0
    if not args.key:
        fail("config explain requires a key, e.g. 'ohbs-image config explain "
             "build.max_build_minutes' (or --all for the full reference).")
        return 1
    explanation = CONFIG_HELP.get(args.key)
    if not explanation:
        fail(f"No explanation available for {args.key}")
        return 1
    print(f"{args.key}: {explanation}")
    return 0


def cmd_config_validate(args: argparse.Namespace) -> int:
    """Roadmap E — local-only validation: parse, load and resolve a config
    without touching the cloud. No credentials or network required.

    Exit codes: 0 valid, 1 invalid configuration, 2 unreadable file.
    """
    path = Path(args.config)
    if not path.exists():
        _validate_report(args, False, [f"Configuration file not found: {path}"])
        return 2
    try:
        data = load_config(path)
    except OSError as exc:
        _validate_report(args, False, [f"Could not read {path}: {exc}"])
        return 2
    except ConfigError as exc:
        _validate_report(args, False, [str(exc)])
        return 1
    try:
        resolve(data)
    except ConfigError as exc:
        _validate_report(args, False, [str(exc)])
        return 1
    _validate_report(args, True, [])
    ok(f"Configuration is valid: {path}")
    return 0


def _validate_report(args: argparse.Namespace, valid: bool,
                     errors: list[str]) -> None:
    if getattr(args, "output", "text") == "json":
        print(json.dumps({"valid": valid, "errors": errors,
                          "config": str(Path(args.config))},
                         ensure_ascii=False, indent=2))
    else:
        for error in errors:
            fail(error)


def cmd_config_diff(args: argparse.Namespace) -> int:
    """Roadmap E — field-level diff of two config files (before/after)."""
    try:
        left = load_config(Path(args.before))
        right = load_config(Path(args.after))
    except (OSError, ConfigError) as exc:
        fail(str(exc))
        return 1
    changes = _diff_tables(left, right)
    if not changes:
        ok("Configurations are identical")
        return 0
    if args.output == "json":
        print(json.dumps({"same": False, "changes": changes},
                         ensure_ascii=False, indent=2))
    else:
        for change in changes:
            warn(f"{change['key']}: {change['before']} -> {change['after']}")
        fail(f"{len(changes)} difference(s) between {args.before} and {args.after}")
    return 1


def _diff_tables(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, str]]:
    """Compare two parsed config dicts; returns [{key, before, after}] rows."""
    changes: list[dict[str, str]] = []
    for section in sorted(set(left) | set(right)):
        a = left.get(section)
        b = right.get(section)
        if a is None or b is None:
            changes.append({"key": section, "before": _fmt(a), "after": _fmt(b)})
            continue
        if not isinstance(a, dict) or not isinstance(b, dict):
            if _fmt(a) != _fmt(b):
                changes.append({"key": section, "before": _fmt(a), "after": _fmt(b)})
            continue
        for key in sorted(set(a) | set(b)):
            va, vb = a.get(key), b.get(key)
            if _fmt(va) != _fmt(vb):
                changes.append({"key": f"{section}.{key}",
                                "before": _fmt(va), "after": _fmt(vb)})
    return changes


def _fmt(value: Any) -> str:
    if value is None:
        return "<absent>"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def cmd_config_get(args: argparse.Namespace) -> int:
    """Roadmap E — print the effective value of one dotted config key after
    resolution, so defaults and overrides are visible."""
    try:
        data = load_config(Path(args.config))
        r = resolve(data)
    except (OSError, ConfigError) as exc:
        fail(str(exc))
        return 1
    attr = _RESOLVED_KEY_MAP.get(args.key)
    if attr is not None:
        value = getattr(r, attr)
    elif args.key in CONFIG_HELP:
        value = _raw_lookup(data, args.key)
    else:
        fail(f"Unknown configuration key: {args.key}")
        return 1
    if getattr(args, "output", "text") == "json":
        print(json.dumps({"key": args.key, "value": _jsonable(value)},
                         ensure_ascii=False, indent=2))
    else:
        print(_fmt(value))
    return 0


def _raw_lookup(data: dict[str, Any], key: str) -> Any:
    section, _, name = key.partition(".")
    value = data.get(section, {}).get(name)
    if value is None:
        return _RAW_DEFAULTS.get(key)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
        return value
    return str(value)


def cmd_config_merge(args: argparse.Namespace) -> int:
    """Roadmap E — deep-merge layered config files and validate the result.

    Exit codes: 0 merged and valid, 1 invalid merged config, 2 unreadable
    file. With --output the merged TOML is written (or a JSON validity
    report with --output-json); otherwise the merged TOML is printed.
    """
    paths = [Path(args.base)] + [Path(p) for p in args.overlays]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        fail("Configuration file not found: " + ", ".join(missing))
        return 2
    try:
        data = load_config_layered(paths)
    except OSError as exc:
        fail(f"Could not read config: {exc}")
        return 2
    except ConfigError as exc:
        fail(str(exc))
        return 1
    if getattr(args, "output_json", False):
        print(json.dumps({"valid": True, "layers": [str(p) for p in paths]},
                         ensure_ascii=False, indent=2))
        return 0
    merged = _toml_dumps(data)
    if args.output:
        Path(args.output).write_text(merged, encoding="utf-8")
        ok(f"Merged configuration -> {args.output}")
    else:
        print(merged, end="")
    ok("Layered configuration merged and valid: " + " < ".join(str(p) for p in paths))
    return 0


def _toml_dumps(data: dict[str, Any]) -> str:
    """Render a parsed config dict back to TOML (schema_version first)."""
    lines: list[str] = []
    scalar_keys = [k for k in data if not isinstance(data[k], dict)]
    if "schema_version" in scalar_keys:
        scalar_keys.remove("schema_version")
        lines.append(f"schema_version = {data['schema_version']}")
        lines.append("")
    for key in scalar_keys:
        lines.append(f"{key} = {_fmt_toml(data[key])}")
    if scalar_keys:
        lines.append("")
    for section, table in data.items():
        if not isinstance(table, dict):
            continue
        if section == "cis":
            # [cis] is a synthetic alias of [ohbs] created during
            # validation; never emit it back into a user config file.
            continue
        lines.append(f"[{section}]")
        for key, value in table.items():
            if isinstance(value, dict):
                lines.append(f"[{section}.{key}]")
                for sub_key, sub_value in value.items():
                    lines.append(f"{sub_key} = {_fmt_toml(sub_value)}")
            else:
                lines.append(f"{key} = {_fmt_toml(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _fmt_toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_toml(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


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
