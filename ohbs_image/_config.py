from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ._catalog import _catalog_basename, _catalog_path, _is_legacy_benchmark
from ._logging import ConfigError, warn
from ._models import BuildSpec, ReleasePolicy, RunContext
from ._profiles import PROFILE_NAMES_HELP, PROFILES


@dataclass
class PackerResult:
    """Normalised return from packer subprocess."""

    exit_code: int
    stdout_lines: list[str] = field(default_factory=list)
    failure_category: str = ""
    retryable: bool = False
    attempts: int = 1

@dataclass
class ResolvedConfig:
    """Fully-resolved build configuration ready for rendering."""

    profile_name: str
    profile: dict[str, Any]
    family: str                         # "" = Linux, "windows" = Windows
    region: str
    zone: str
    instance_type: str
    source_image_id: str
    vpc_id: str
    subnet_id: str
    security_group_id: str
    associate_public_ip: bool
    ssh_port: int
    ssh_timeout: str
    ssh_username: str
    ssh_debug_password: str
    winrm_username: str
    winrm_password_env: str
    image_name_prefix: str
    image_name_override: str            # [image].name — fixed image name ("" = auto)
    instance_name: str                  # [build].instance_name — build CVM instance name ("" = plugin auto)
    image_copy_regions: list[str]
    image_share_accounts: list[str]     # [image].share_accounts — share built image with other uins
    image_share_org_units: list[str]    # [image].share_org_units — parsed but skipped at build time (no org-unit share API)
    spot: bool                          # [build].spot — use a spot instance for the build VM (default false)
    max_build_minutes: int              # [build].max_build_minutes — hard wall-clock limit for Packer runs (default 120)
    cis_level_tag: str
    secret_id_env: str
    secret_key_env: str
    security_token_env: str         # [cloud].security_token_env — STS session token env (default "TENCENTCLOUD_SECURITY_TOKEN")
    assume_role_arn: str               # [cloud].assume_role_arn — group-account CAM role ("" = off)
    assume_role_session: str           # [cloud].assume_role_session (default "ohbs-image")
    assume_role_duration: int          # [cloud].assume_role_duration (default 7200, 0-43200)
    image_os_tag: str
    image_benchmark: str
    catalog_basename: str              # rules.json or rules_<slug>.json — which catalog the build uses
    level: int
    min_score: int                      # [ohbs].min_score — post-reboot audit gate, 0 disables (default 85)
    allow_disruptive: bool              # [ohbs].allow_disruptive — apply disruptive remediations during the build (default true)
    allow_scoped_approval: bool         # [ohbs].allow_scoped_approval — explicitly permit a rule-subset image
    role_dir: str
    smoke_test: bool                    # [meta].smoke_test — run instance-level smoke checks before snapshot (default true)
    cve_scan: bool                      # [meta].cve_scan — trivy vulnerability scan gate before snapshot (default false)
    sbom: bool                          # [meta].sbom — emit SBOM into image + provenance (default false)
    delivery_report_required: bool       # [meta].delivery_report_required — fail release if HTML delivery report cannot be written
    rules_include: list[str]            # [ohbs].rules_include — rule-id filter (empty = all)
    rules_exclude: list[str]            # [ohbs].rules_exclude — rule-id filter (wins over include)
    rules_overrides: dict[str, dict[str, Any]]    # [ohbs.overrides] — per-rule param deep-merge (rule_id -> {param: value})
    notify_webhook: str                 # [notify].webhook — WeCom group-robot webhook URL ("" = off)
    notify_on: str                      # [notify].on — "always" | "success" | "failure" (default "failure")
    deploy_webhook: str                 # [notify].deploy_webhook — POST image metadata on build success ("" = off)
    sign_key: str                       # [sign].gpg_key — GPG key id/fingerprint for SLSA provenance signing ("" = off)
    attestation_required: bool          # [attestation].required — signed provenance is required before release
    test_components: list[str]          # [meta].test_components — user-defined test scripts run before snapshot
    verify_boot: bool                   # [meta].verify_boot — boot a probe instance from the produced image and re-audit before declaring success (default false)
    packer_extra: dict[str, Any] = field(default_factory=dict)  # [build.packer] — arbitrary packer tencentcloud-cvm builder args (passthrough)
    run_id: str = ""                    # runtime-only evidence correlation ID (not read from TOML)

    @property
    def build_spec(self) -> BuildSpec:
        """Immutable image-defining view for fingerprints and manifests."""
        return BuildSpec(self.profile_name, self.region, self.zone, self.instance_type,
                         self.source_image_id, self.image_benchmark, self.level,
                         self.image_os_tag, self.catalog_basename)

    @property
    def release_policy(self) -> ReleasePolicy:
        """Immutable release-gate view, independent of provider placement."""
        return ReleasePolicy(self.min_score, self.attestation_required,
                             self.delivery_report_required, self.verify_boot,
                             self.allow_scoped_approval)

    @property
    def run_context(self) -> RunContext:
        """Snapshot the mutable runtime-only fields for evidence output."""
        return RunContext(self.run_id, self.max_build_minutes)

def _validate_value_present(label: str, value: Any) -> str | None:
    """Return an error message if *value* looks like a placeholder, else None."""
    if value is None or (isinstance(value, str) and not value):
        return f"{label}: cannot be empty"
    if (isinstance(value, str)
            and re.search(r"(?<![0-9a-f])x{8,}(?![0-9a-f])", value, re.IGNORECASE)):
        return f"{label}: still placeholder '{value}'"
    return None


def _get_table(data: dict[str, Any], section: str) -> dict[str, Any]:
    """Return a config section, requiring it to be a TOML table."""
    value = data.get(section, {})
    if not isinstance(value, dict):
        raise ConfigError(
            f"[{section}] must be a table, got {type(value).__name__}.")
    return value


def _read_bool(data: dict[str, Any], section: str, key: str, default: bool) -> bool:
    """Read a TOML boolean without silently coercing strings or integers."""
    value = _get_table(data, section).get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(
            f"[{section}].{key} must be a boolean, got "
            f"{type(value).__name__}. Use true/false without quotes."
        )
    return value


def _read_int(data: dict[str, Any], section: str, key: str, default: int) -> int:
    """Read a TOML integer without silently truncating floats or accepting bools."""
    value = _get_table(data, section).get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"[{section}].{key} must be an integer, got "
            f"{type(value).__name__}. Use a plain number without quotes or decimals."
        )
    return value


def _read_str_list(data: dict[str, Any], section: str, key: str) -> list[str]:
    """Read a TOML list containing only non-empty strings."""
    raw = _get_table(data, section).get(key, [])
    if not isinstance(raw, list):
        raise ConfigError(
            f"[{section}].{key} must be a list, got {type(raw).__name__}.")
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str):
            raise ConfigError(
                f"[{section}].{key}[{index}] must be a string, got "
                f"{type(value).__name__}.")
        cleaned = value.strip()
        if not cleaned:
            raise ConfigError(f"[{section}].{key}[{index}] must not be empty.")
        values.append(cleaned)
    return values


def _read_required_str(data: dict[str, Any], section: str, key: str) -> str:
    """Read a required, non-empty TOML string without coercing other types."""
    value = _get_table(data, section).get(key)
    if not isinstance(value, str):
        raise ConfigError(f"[{section}].{key} must be a string, got {type(value).__name__}.")
    cleaned = value.strip()
    if not cleaned:
        raise ConfigError(f"[{section}].{key} must not be empty.")
    return cleaned


def _validate_https_url(value: str, label: str) -> str:
    """Validate an optional notification endpoint without permitting SSRF-by-default."""
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError(f"{label} must be an absolute https URL.")
    if parsed.username or parsed.password:
        raise ConfigError(f"{label} must not contain URL credentials.")
    # DNS names remain an administrator-controlled integration boundary, but
    # never allow an explicit literal address to target a host-local, private,
    # link-local, multicast, or otherwise non-public endpoint.
    try:
        host = ip_address(parsed.hostname)
    except ValueError:
        return value
    if not host.is_global:
        raise ConfigError(f"{label} must not use a non-public IP address.")
    return value


def _parse_config(path: Path) -> dict[str, Any]:
    """Parse one config file (no validation). Raises ConfigError."""
    if not path.exists():
        raise ConfigError(
            f"Configuration file not found: {path}\n"
            f"  Run 'ohbs-image init' to generate a template."
        )

    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except Exception as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` into ``base`` and return a new dict.

    Roadmap E — overlay semantics: tables merge recursively (later layers
    win per key); lists and scalars are REPLACED by the overlay (a later
    layer's list is authoritative, not appended). Neither input is
    mutated.
    """
    result = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def load_config_layered(paths: Sequence[Path]) -> dict[str, Any]:
    """Load and merge multiple config files, then validate the result.

    Roadmap E — layered configuration: files are parsed in order and
    deep-merged so each later file overrides the earlier ones key-by-key
    (see :func:`deep_merge`). The merged dict then goes through exactly
    the same validation as :func:`load_config`, so a layer that leaves a
    required field unset still fails loudly. Raises ConfigError on any
    missing/unparseable file or an invalid merged result.
    """
    if not paths:
        raise ConfigError("No configuration files given.")
    merged: dict[str, Any] = {}
    for path in paths:
        merged = deep_merge(merged, _parse_config(path))
    return _validate_config(merged)


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate ohbs-image.toml.  Raises ConfigError on invalid input."""
    return _validate_config(_parse_config(path))


def _validate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed config dict (schema gate, required fields, types)."""
    # Roadmap E — schema versioning: the version gate is read here so a
    # config written by a newer ohbs-image fails loudly instead of being
    # silently mis-resolved. Missing schema_version means v1 (legacy).
    schema_version = data.get("schema_version", 1)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ConfigError(
            f"schema_version must be an integer, got {schema_version!r}.")
    if schema_version < 1:
        raise ConfigError(
            f"schema_version {schema_version} is invalid (minimum 1).")
    if schema_version > 1:
        raise ConfigError(
            f"Configuration schema_version {schema_version} is newer than "
            f"this ohbs-image supports (max 1). Upgrade ohbs-image or run "
            f"'ohbs-image config migrate'.")

    # Backward-compat: accept either [ohbs] (new) or [cis] (legacy) as the
    # hardening section. New configs should use [ohbs]; [cis] keeps working.
    if "ohbs" in data and "cis" not in data:
        data["cis"] = data["ohbs"]
    elif "cis" in data and "ohbs" not in data:
        data["ohbs"] = data["cis"]
    elif ("ohbs" in data and "cis" in data
          and data["ohbs"] is not data["cis"]):
        # load_config deliberately creates an in-memory alias so legacy
        # internal readers can use [cis]. Do not warn about that synthetic
        # alias; warn only when the caller supplied two distinct sections.
        warn("Both [ohbs] and [cis] sections are present; "
             "[ohbs] takes precedence and [cis] is ignored.")
        data["cis"] = data["ohbs"]

    required: dict[str, list[str]] = {
        "build": [
            "profile", "region", "zone", "instance_type", "source_image_id",
            "vpc_id", "subnet_id", "security_group_id", "associate_public_ip",
        ],
        "image": ["name_prefix", "copy_regions"],
        "ohbs": ["level"],
        "cloud": ["secret_id_env", "secret_key_env"],
    }

    for section, keys in required.items():
        if section not in data:
            raise ConfigError(f"Missing [{section}] section in configuration")
        if not isinstance(data[section], dict):
            raise ConfigError(
                f"[{section}] must be a table, got {type(data[section]).__name__}.")
        for key in keys:
            if key not in data[section]:
                raise ConfigError(f"Missing field: [{section}].{key}")

    # These flow directly into cloud API calls and HCL. Reject type coercion
    # here so an accidental boolean/integer produces a precise config error,
    # not a remote Packer failure later in the build.
    for section, key in [
        ("build", "profile"), ("build", "region"), ("build", "zone"),
        ("build", "instance_type"), ("build", "source_image_id"),
        ("build", "vpc_id"), ("build", "subnet_id"),
        ("build", "security_group_id"), ("image", "name_prefix"),
        ("cloud", "secret_id_env"), ("cloud", "secret_key_env"),
    ]:
        _read_required_str(data, section, key)

    profile_name = _read_required_str(data, "build", "profile")
    if profile_name not in PROFILES:
        raise ConfigError(
            f"Unknown profile: {profile_name}\n"
            f"  Valid choices: {PROFILE_NAMES_HELP}"
        )

    level = data["ohbs"]["level"]
    if not isinstance(level, int) or isinstance(level, bool) or level not in (1, 2):
        raise ConfigError(f"[ohbs].level must be 1 or 2, got: {level}")

    itype = _read_required_str(data, "build", "instance_type")
    if "." not in itype:
        raise ConfigError(
            f"[build].instance_type '{itype}' is missing the CVM prefix.\n"
            f"  Use the full specifier, e.g. 'S5.MEDIUM2' (not 'S5-MEDIUM2')."
        )

    if not str(data["build"]["security_group_id"]).startswith("sg-"):
        warn(f"[build].security_group_id '{data['build']['security_group_id']}' "
             f"does not look like a security group ID (should start with 'sg-').")

    if not isinstance(data["build"]["associate_public_ip"], bool):
        raise ConfigError(
            f"[build].associate_public_ip must be a boolean, got "
            f"{type(data['build']['associate_public_ip']).__name__}. "
            f"Use true/false without quotes."
        )

    copy_regions_raw = data["image"]["copy_regions"]
    if not isinstance(copy_regions_raw, list):
        raise ConfigError(
            f"[image].copy_regions must be a list, got {type(copy_regions_raw).__name__}."
        )
    for index, region in enumerate(copy_regions_raw):
        if not isinstance(region, str) or not region.strip():
            raise ConfigError(f"[image].copy_regions[{index}] must be a non-empty string.")
        region_str = region.strip()
        if not region_str or not all(c.isalnum() or c == "-" for c in region_str):
            warn(f"[image].copy_regions entry '{region_str}' does not look like a Tencent region code.")

    for label, key, prefix in [
        ("source image ID", "source_image_id", "img-"),
        ("VPC ID", "vpc_id", "vpc-"),
        ("subnet ID", "subnet_id", "subnet-"),
    ]:
        val = _read_required_str(data, "build", key)
        if not val.startswith(prefix):
            warn(f"[build].{key} '{val}' does not look like a {label} (should start with '{prefix}').")

    return data

_CIS_REGION_DASHES = str.maketrans({
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2212": "-",  # minus sign
    "\uFF0D": "-",  # fullwidth hyphen-minus
})

def _sanitize_region_zone(value: str, label: str) -> str:
    """Replace non-ASCII dashes with regular ASCII hyphen '-'."""
    cleaned = str(value).translate(_CIS_REGION_DASHES)
    if cleaned != value:
        warn(f"{label} '{value}' contains a non-ASCII dash — "
             f"auto-corrected to '{cleaned}'")
    return cleaned

def resolve(data: dict[str, Any]) -> ResolvedConfig:
    """Flatten raw config + profile lookup into a ResolvedConfig."""
    # Backward-compat: ensure both [ohbs] and [cis] sections are present.
    if "ohbs" in data and "cis" not in data:
        data["cis"] = data["ohbs"]
    elif "cis" in data and "ohbs" not in data:
        data["ohbs"] = data["cis"]
    elif ("ohbs" in data and "cis" in data
          and data["ohbs"] is not data["cis"]):
        warn("Both [ohbs] and [cis] sections are present; "
             "[ohbs] takes precedence and [cis] is ignored.")
        data["cis"] = data["ohbs"]
    profile_name = _read_required_str(data, "build", "profile")
    p = PROFILES[profile_name]
    meta: dict[str, Any] = _get_table(data, "meta")
    level: int = int(data["ohbs"]["level"])
    family: str = str(p.get("family", ""))

    copy_regions_raw = data["image"]["copy_regions"]
    if not isinstance(copy_regions_raw, list):
        raise ConfigError(
            f"[image].copy_regions must be a list, got {type(copy_regions_raw).__name__}. "
            f"Use [] for no copy or ['ap-shanghai'] for cross-region copy."
        )
    copy_regions = [_sanitize_region_zone(r.strip(), "[image].copy_regions") for r in copy_regions_raw]

    # Explicit None checks — `or` would silently discard a configured 0.
    _ssh_port_raw = meta.get("ssh_port")
    if _ssh_port_raw in (None, ""):
        _ssh_port_raw = p.get("ssh_port", 22)
    if not isinstance(_ssh_port_raw, int) or isinstance(_ssh_port_raw, bool):
        raise ConfigError(
            f"[meta].ssh_port must be an integer, got {type(_ssh_port_raw).__name__}.")
    ssh_port = _ssh_port_raw
    if not (1 <= ssh_port <= 65535):
        raise ConfigError(f"[meta].ssh_port must be 1-65535, got {ssh_port}")
    ssh_timeout = str(meta.get("ssh_timeout") or p.get("ssh_timeout") or "15m")

    # [image].name — optional fixed image name; empty means auto-generate.
    image_name_override = str(data.get("image", {}).get("name", "")).strip()
    if image_name_override:
        if len(image_name_override) < 1 or len(image_name_override) > 60:
            raise ConfigError(
                f"[image].name must be 1-60 characters, got {len(image_name_override)}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", image_name_override):
            raise ConfigError(
                f"[image].name contains invalid characters: {image_name_override!r}. "
                "Use letters, digits, dot, dash, underscore only.")

    # [build].instance_name — optional explicit name for the temporary build
    # CVM (the machine Packer launches and hardens before snapshotting). Empty
    # means the Packer plugin auto-generates it. Used by the E2E runner to tag
    # target machines with a recognizable CIS_E2E_* prefix.
    instance_name = str(data.get("build", {}).get("instance_name", "")).strip()
    if instance_name:
        if len(instance_name) > 60:
            raise ConfigError(
                f"[build].instance_name must be <= 60 characters, "
                f"got {len(instance_name)}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", instance_name):
            raise ConfigError(
                f"[build].instance_name contains invalid characters: {instance_name!r}. "
                "Use letters, digits, dot, dash, underscore only.")
        # Tencent's CVM builder derives the instance HOSTNAME from this name:
        # it lowercases it, converts '_' -> '-', and truncates to 15 chars. A
        # name cut at a '_'/'-' leaves a trailing '-' which Tencent rejects
        # (InvalidParameterValue.IllegalHostName). E.g. "CIS_E2E_rhel10_L1"
        # -> "cis-e2e-rhel10-" (illegal). Only rewrite when there is an actual
        # risk; short names that survive unchanged are left alone.
        host = instance_name.lower().replace("_", "-")
        host = re.sub(r"[^a-z0-9-]+", "-", host)
        host = host.strip("-.")
        truncated = host[:15]
        if truncated.endswith("-") or truncated.endswith("."):
            if len(host) > 14:
                host = host[:14]
            host = host.rstrip("-.")
            warn(f"[build].instance_name '{instance_name}' sanitized to '{host}' "
                 f"for a valid CVM hostname")
            instance_name = host

    # [build.packer] — passthrough of arbitrary packer tencentcloud-cvm builder
    # args (e.g. disk_type, disk_size, data_disks, internet_max_bandwidth_out).
    # The user's own toml is trusted; value legality is enforced at render time
    # by _format_hcl_value. This lets ohbs-image inherit the full packer
    # capability set without hardcoding each argument.
    _packer_raw = _get_table(data, "build").get("packer", {})
    if not isinstance(_packer_raw, dict):
        raise ConfigError(
            f"[build.packer] must be a table, got {type(_packer_raw).__name__}.")
    packer_extra = dict(_packer_raw)
    for k in packer_extra:
        if not isinstance(k, str):
            raise ConfigError("[build.packer] keys must be strings")
        # Keys are emitted verbatim into the HCL source block
        # (f"  {k} = ...") — restrict them to plain identifiers so a
        # crafted TOML quoted-key can't inject arbitrary HCL (the values
        # are already escaped by _format_hcl_value; keys were not).
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise ConfigError(
                f"[build.packer] key {k!r} is not a valid HCL identifier. "
                "Use letters, digits and underscores only.")

    # [cloud].assume_role_* — group-account (organization) cross-account builds.
    # When set, Packer assumes the target account's CAM role with the local
    # AK/SK before launching the build instance.
    assume_role_arn = str(data.get("cloud", {}).get("assume_role_arn", "")).strip()
    if assume_role_arn and not re.fullmatch(r"[A-Za-z0-9:_/-]+", assume_role_arn):
        raise ConfigError(
            f"[cloud].assume_role_arn contains invalid characters: "
            f"{assume_role_arn!r}. Expected a CAM role ARN like "
            "qcs::cam::uin/12345:roleName/CrossAccountBuilder")
    assume_role_session = str(data.get("cloud", {}).get("assume_role_session", "ohbs-image")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_=,.@-]+", assume_role_session):
        raise ConfigError(
            f"[cloud].assume_role_session contains invalid characters: {assume_role_session!r}")
    assume_role_duration = _read_int(data, "cloud", "assume_role_duration", 7200)
    if not (0 <= assume_role_duration <= 43200):
        raise ConfigError(
            f"[cloud].assume_role_duration must be 0-43200, got {assume_role_duration}")

    # [meta].smoke_test — instance-level checks before the snapshot (default on).
    smoke_test = _read_bool(data, "meta", "smoke_test", True)

    # [notify] — WeCom group-robot webhook; empty webhook disables notifications.
    notify = _get_table(data, "notify")
    notify_webhook = _validate_https_url(str(notify.get("webhook", "")).strip(),
                                         "[notify].webhook")
    notify_on = str(notify.get("on", "failure")).strip().lower()
    if notify_on not in ("always", "success", "failure"):
        raise ConfigError(
            f"[notify].on must be one of always|success|failure, got {notify_on!r}")

    # [sign] — GPG key for SLSA-style provenance signing ("" = off).
    sign_key = str(_get_table(data, "sign").get("gpg_key", "")).strip()
    # When a signing key is configured, treating a failed signature as a
    # successful production build is unsafe.  Operators can explicitly opt
    # out for local development, but secure behaviour is the default.
    attestation_required = _read_bool(data, "attestation", "required", bool(sign_key))
    if attestation_required and not sign_key:
        raise ConfigError("[attestation].required = true requires [sign].gpg_key.")

    # [ohbs].rules_include / rules_exclude — optional rule-id filters.
    rules_include = _read_str_list(data, "ohbs", "rules_include")
    rules_exclude = _read_str_list(data, "ohbs", "rules_exclude")
    if rules_include and rules_exclude:
        overlap = sorted(set(rules_include) & set(rules_exclude))
        if overlap:
            raise ConfigError(
                f"[ohbs] rules_include and rules_exclude overlap: {overlap}")

    # [ohbs.overrides] — per-rule parameter deep-merge (rule_id -> {param: value}).
    # Mirrors ansible-lockdown's per-control vars: tune a rule's parameters
    # without editing the bundled catalog.  Keys must be dotted rule IDs.
    overrides_raw = data.get("ohbs", {}).get("overrides", {})
    if not isinstance(overrides_raw, dict):
        raise ConfigError(
            f"[ohbs].overrides must be a table of rule_id -> params, got "
            f"{type(overrides_raw).__name__}.")
    rules_overrides: dict[str, dict[str, Any]] = {}
    for rid, params in overrides_raw.items():
        rid = str(rid).strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", rid):
            raise ConfigError(
                f"[ohbs].overrides key {rid!r} is not a dotted CIS rule ID "
                "(e.g. \"5.2.2\").")
        if not isinstance(params, dict):
            raise ConfigError(
                f"[ohbs].overrides.{rid} must be a table of parameter values, "
                f"got {type(params).__name__}.")
        rules_overrides[rid] = {str(k): v for k, v in params.items()}

    # [meta].cve_scan / [meta].sbom — optional supply-chain gates.
    cve_scan = _read_bool(data, "meta", "cve_scan", False)
    sbom = _read_bool(data, "meta", "sbom", False)
    delivery_report_required = _read_bool(data, "meta", "delivery_report_required", False)

    # [image].share_accounts — cross-account image sharing (empty = off).
    share_accounts = _read_str_list(data, "image", "share_accounts")
    for acc in share_accounts:
        if not re.fullmatch(r"uin/[0-9]+", acc):
            raise ConfigError(
                f"[image].share_accounts entry {acc!r} is not a valid "
                "Tencent Cloud account ID (expected \"uin/1234567890\").")

    # [image].share_org_units — parsed for forward compatibility, but
    # ModifyImageSharePermission accepts account IDs only, so cmd_build warns
    # and skips these (no org-unit sharing API is wired up).
    share_org_units = _read_str_list(data, "image", "share_org_units")
    for acc in share_org_units:
        if not re.fullmatch(r"uin/[0-9]+", acc):
            raise ConfigError(
                f"[image].share_org_units entry {acc!r} is not a valid "
                "Tencent Cloud account ID (expected \"uin/1234567890\").")

    # [build].spot — spot instance for the build VM (up to ~90% cheaper).
    spot = _read_bool(data, "build", "spot", False)

    # [build].max_build_minutes — a cloud build can otherwise remain alive
    # indefinitely after a provider or communicator stall.  run_packer owns
    # the termination/kill sequence; config owns the explicit budget.
    max_build_minutes = _read_int(data, "build", "max_build_minutes", 120)
    if not (15 <= max_build_minutes <= 1440):
        raise ConfigError(
            f"[build].max_build_minutes must be 15-1440, got {max_build_minutes}. "
            "Use a value long enough for the profile and short enough to cap cost.")

    # [meta].test_components — user-defined test scripts run before snapshot.
    test_components = _read_str_list(data, "meta", "test_components")

    # [meta].verify_boot — clean-boot verification after the snapshot.
    verify_boot = _read_bool(data, "meta", "verify_boot", False)

    # [ohbs].min_score — post-reboot audit gate (0 disables; default 85).
    min_score = _read_int(data, "ohbs", "min_score", 85)
    if not (0 <= min_score <= 100):
        raise ConfigError(
            f"[ohbs].min_score must be 0-100, got {min_score}. "
            "0 disables the gate; 85 is the default.")

    # [ohbs].allow_disruptive — apply disruptive remediations during the
    # build.  Default true: the build VM is ephemeral and rebooted before
    # the audit, so disruptive fixes (mount options, service removals, …)
    # are safe here and skipping them only lowers the image's score.
    allow_disruptive = _read_bool(data, "ohbs", "allow_disruptive", True)
    allow_scoped_approval = _read_bool(data, "ohbs", "allow_scoped_approval", False)

    benchmark = str(meta.get("benchmark", p.get("benchmark", "")))
    catalog_basename = _catalog_basename(role_dir=str(p["role_dir"]), benchmark=benchmark)
    if (not _is_legacy_benchmark(benchmark.strip().lower())
            and not _catalog_path(str(p["role_dir"]), benchmark).is_file()):
        raise ConfigError(
            f"No catalog bundled for benchmark {benchmark!r} on role "
            f"{p['role_dir']!r}; expected {catalog_basename}."
        )

    return ResolvedConfig(
        profile_name=profile_name,
        profile=p,
        family=family,
        region=_sanitize_region_zone(_read_required_str(data, "build", "region"), "[build].region"),
        zone=_sanitize_region_zone(_read_required_str(data, "build", "zone"), "[build].zone"),
        instance_type=_read_required_str(data, "build", "instance_type"),
        source_image_id=_read_required_str(data, "build", "source_image_id"),
        vpc_id=_read_required_str(data, "build", "vpc_id"),
        subnet_id=_read_required_str(data, "build", "subnet_id"),
        security_group_id=_read_required_str(data, "build", "security_group_id"),
        associate_public_ip=bool(data["build"]["associate_public_ip"]),
        ssh_port=ssh_port,
        ssh_timeout=ssh_timeout,
        ssh_username=str(p.get("ssh_username", "")),
        ssh_debug_password=str(meta.get("ssh_debug_password", "")),
        winrm_username=str(p.get("winrm_username", "")),
        winrm_password_env=str(data.get("cloud", {}).get(
            "winrm_password_env",
            "WINRM_PASSWORD" if family == "windows" else "")),
        image_name_prefix=_read_required_str(data, "image", "name_prefix"),
        image_name_override=image_name_override,
        instance_name=instance_name,
        packer_extra=packer_extra,
        image_copy_regions=copy_regions,
        image_share_accounts=share_accounts,
        image_share_org_units=share_org_units,
        spot=spot,
        max_build_minutes=max_build_minutes,
        cis_level_tag=f"level{level}-server",
        secret_id_env=_read_required_str(data, "cloud", "secret_id_env"),
        secret_key_env=_read_required_str(data, "cloud", "secret_key_env"),
        security_token_env=str(data.get("cloud", {}).get("security_token_env", "TENCENTCLOUD_SECURITY_TOKEN")),
        assume_role_arn=assume_role_arn,
        assume_role_session=assume_role_session,
        assume_role_duration=assume_role_duration,
        image_os_tag=str(meta.get("os_tag", p.get("os_tag", ""))),
        image_benchmark=benchmark,
        catalog_basename=catalog_basename,
        level=level,
        role_dir=str(p["role_dir"]),
        smoke_test=smoke_test,
        cve_scan=cve_scan,
        sbom=sbom,
        delivery_report_required=delivery_report_required,
        rules_include=rules_include,
        rules_exclude=rules_exclude,
        rules_overrides=rules_overrides,
        min_score=min_score,
        allow_disruptive=allow_disruptive,
        allow_scoped_approval=allow_scoped_approval,
        notify_webhook=notify_webhook,
        notify_on=notify_on,
        deploy_webhook=_validate_https_url(
            str(data.get("notify", {}).get("deploy_webhook", "")).strip(),
            "[notify].deploy_webhook"),
        sign_key=sign_key,
        attestation_required=attestation_required,
        test_components=test_components,
        verify_boot=verify_boot,
    )

def _lineage_path() -> Path:
    return _state_dir() / "lineage.jsonl"

def _reports_dir() -> Path:
    return _state_dir() / "reports"


def _state_dir() -> Path:
    """Return the evidence root, configurable for isolated CI/team storage."""
    raw = os.environ.get("OHBS_IMAGE_STATE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".ohbs-image"
