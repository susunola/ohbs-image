"""ohbs-image quickstart — zero-to-first-image in one command.

``quickstart`` provisions a temporary build environment (VPC, subnet,
security group), selects the newest matching source image and an in-stock
instance type, writes a minimal ``ohbs-image.toml``, then chains
doctor -> plan and hands off to build.

Everything quickstart creates is tagged ``managed_by=ohbs-image`` /
``ephemeral=true`` / ``purpose=quickstart`` and recorded in
``<target>.quickstart.json`` so it can be torn down later with
``ohbs-image quickstart --cleanup``.

Usage (all cloud calls are read-only except the explicit provisioning
Create* actions and the --cleanup Delete* actions):

    ohbs-image quickstart --region ap-guangzhou --profile ubuntu2204 \
        --ingress-cidr 203.0.113.10/32
    ohbs-image quickstart --region ap-guangzhou --profile ubuntu2204 \
        --ingress-cidr 203.0.113.10/32 --dry-run
    ohbs-image quickstart --cleanup
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import time
from pathlib import Path
from typing import Any

import ohbs_image

from ._discover import discover_resources
from ._logging import ConfigError, banner, fail, info, ok, warn
from ._onboarding import _render_config_toml
from ._profiles import PROFILES

# Deterministic per-region VPC CIDR pool: repeated quickstarts in one region
# reuse the same range, so a stale leftover VPC is easy to recognise.
_VPC_CIDR_POOL = [f"10.{i}.0.0/16" for i in range(20, 28)]

_QS_TAGS = [
    {"Key": "managed_by", "Value": "ohbs-image"},
    {"Key": "ephemeral", "Value": "true"},
    {"Key": "purpose", "Value": "quickstart"},
]

_QS_SCHEMA = "https://ohbs-image.dev/quickstart-resources/v1"


def _creds() -> tuple[str, str, str | None]:
    sid, key, tok = ohbs_image._creds(
        "TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY",
        "TENCENTCLOUD_SECURITY_TOKEN")
    if not sid or not key:
        raise ConfigError(
            "TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY must be set "
            "before running quickstart (export them, or use a CI OIDC role)")
    return sid, key, tok


def _pick_zone(region: str, sid: str, key: str, tok: str | None) -> str:
    """First AVAILABLE zone in *region* (read-only DescribeZones)."""
    raw = ohbs_image._tc3_api("cvm", "DescribeZones", "2017-03-12", region, {},
                              sid, key, tok)
    rows = raw.get("Response", {}).get("ZoneSet", []) or []
    available = [str(z.get("Zone", "")) for z in rows
                 if isinstance(z, dict)
                 and "AVAILABLE" in str(z.get("ZoneState", "")).upper()]
    if not available:
        raise ConfigError(f"no available zones found in {region} — pass --zone explicitly")
    return available[0]


def _pick_source_image(region: str, profile: str) -> str:
    """Newest matching public source image for *profile* (read-only)."""
    rows = discover_resources("images", region, profile=profile)
    usable = [r for r in rows if r.get("id")
              and str(r.get("state", "")).upper() != "UNAVAILABLE"]
    if not usable:
        raise ConfigError(
            f"no matching source image for profile {profile} in {region} "
            f"(run: ohbs-image discover images --region {region} "
            f"--profile {profile})")
    return str(max(usable, key=lambda r: str(r.get("created_at", "")))["id"])


def _pick_instance_type(region: str, zone: str, profile: str) -> str:
    """First in-stock S5 type; falls back to the historical default."""
    family = PROFILES[profile].get("family", "")
    default = "S5.LARGE4" if family == "windows" else "S5.MEDIUM2"
    try:
        rows = discover_resources("instance-types", region, zone=zone,
                                  in_stock=True)
    except OSError:
        rows = []
    for row in rows:
        if str(row.get("id", "")).startswith("S5."):
            return str(row["id"])
    if rows:
        return str(rows[0]["id"])
    info(f"No in-stock instance type discovered — using default {default}")
    return default


def _vpc_cidr_for(region: str) -> str:
    index = sum(ord(ch) for ch in region) % len(_VPC_CIDR_POOL)
    return _VPC_CIDR_POOL[index]


def _create_vpc(region: str, sid: str, key: str, tok: str | None) -> str:
    cidr = _vpc_cidr_for(region)
    raw = ohbs_image._tc3_api("vpc", "CreateVpc", "2017-03-12", region, {
        "VpcName": "ohbs-image-quickstart",
        "CidrBlock": cidr,
        "EnableMulticast": False,
        "Tags": _QS_TAGS,
    }, sid, key, tok)
    return str(raw["Response"]["Vpc"]["VpcId"])


def _create_subnet(region: str, zone: str, vpc_id: str,
                   sid: str, key: str, tok: str | None) -> str:
    # First /20 of the quickstart /16 — room for the build CVM + probes.
    subnet_cidr = _vpc_cidr_for(region).replace("/16", "/20")
    raw = ohbs_image._tc3_api("vpc", "CreateSubnet", "2017-03-12", region, {
        "VpcId": vpc_id,
        "SubnetName": "ohbs-image-quickstart",
        "CidrBlock": subnet_cidr,
        "Zone": zone,
        "Tags": _QS_TAGS,
    }, sid, key, tok)
    return str(raw["Response"]["Subnet"]["SubnetId"])


def _normalize_ingress_cidr(value: str) -> str:
    """Return one explicit IPv4 network; refuse internet-wide ingress."""
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ConfigError(f"invalid --ingress-cidr {value!r}: {exc}") from exc
    if network.version != 4:
        raise ConfigError("--ingress-cidr must be an IPv4 address or CIDR")
    if network.prefixlen == 0:
        raise ConfigError("--ingress-cidr must not expose the build ports to 0.0.0.0/0")
    return str(network)


def _security_group_ingress(profile: str, cidr: str) -> list[dict[str, Any]]:
    """Minimal packer-facing ingress: SSH 22 (Linux) / WinRM 5985-5986 (Windows)."""
    family = PROFILES[profile].get("family", "")
    ports = ["5985", "5986"] if family == "windows" else ["22"]
    return [{"Protocol": "TCP", "Port": port, "CidrBlock": cidr,
             "Action": "ACCEPT"} for port in ports]


def _create_security_group(region: str, sid: str, key: str,
                           tok: str | None) -> str:
    raw = ohbs_image._tc3_api("vpc", "CreateSecurityGroup", "2017-03-12",
                              region, {
                                  "GroupName": "ohbs-image-quickstart",
                                  "GroupDescription": "Ephemeral build SG "
                                                      "created by ohbs-image "
                                                      "quickstart",
                                  "Tags": _QS_TAGS,
                              }, sid, key, tok)
    return str(raw["Response"]["SecurityGroup"]["SecurityGroupId"])


def _configure_security_group(region: str, sg: str, profile: str, cidr: str,
                              sid: str, key: str, tok: str | None) -> None:
    ohbs_image._tc3_api("vpc", "CreateSecurityGroupPolicies", "2017-03-12",
                        region, {
                            "SecurityGroupId": sg,
                            "SecurityGroupPolicySet": {
                                "Ingress": _security_group_ingress(profile, cidr)},
                        }, sid, key, tok)


def _resource_path(target: Path) -> Path:
    """Record file sits next to the config: ``<target>.quickstart.json``."""
    return Path(str(target) + ".quickstart.json")


def _record_resources(target: Path, region: str,
                      resources: dict[str, str]) -> None:
    payload = {
        "schema": _QS_SCHEMA,
        "region": region,
        "resources": resources,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _resource_path(target).parent.mkdir(parents=True, exist_ok=True)
    _resource_path(target).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _cmd_cleanup(target: Path) -> int:
    """Delete resources recorded by a previous quickstart (SG -> subnet -> VPC)."""
    resource_file = _resource_path(target)
    if not resource_file.exists():
        fail(f"No quickstart resource record found: {resource_file}")
        return 1
    try:
        payload = json.loads(resource_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Could not read {resource_file}: {exc}")
        return 1
    region = str(payload.get("region", "")) if isinstance(payload, dict) else ""
    resources = payload.get("resources", {}) if isinstance(payload, dict) else {}
    if not region:
        fail(f"Resource record has no region: {resource_file}")
        return 1
    try:
        sid, key, tok = _creds()
    except ConfigError as exc:
        fail(str(exc))
        return 2
    steps: list[tuple[str, str, dict[str, str]]] = []
    sg = resources.get("security_group_id", "")
    subnet = resources.get("subnet_id", "")
    vpc = resources.get("vpc_id", "")
    if sg:
        steps.append(("security group", "DeleteSecurityGroup",
                      {"SecurityGroupId": sg}))
    if subnet:
        steps.append(("subnet", "DeleteSubnet", {"SubnetId": subnet}))
    if vpc:
        steps.append(("VPC", "DeleteVpc", {"VpcId": vpc}))
    if not steps:
        warn("Resource record contains nothing to delete")
        return 0
    all_gone = True
    for label, action, params in steps:
        try:
            ohbs_image._tc3_api("vpc", action, "2017-03-12", region, params,
                                sid, key, tok)
        except ConfigError as exc:
            warn(f"Could not delete {label}: {exc}")
            all_gone = False
        else:
            ok(f"Deleted {label}")
    if all_gone:
        resource_file.unlink(missing_ok=True)
        ok(f"Removed record: {resource_file}")
    return 0 if all_gone else 1


def cmd_quickstart(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser()
    if getattr(args, "cleanup", False):
        return _cmd_cleanup(target)

    if target.exists() and not args.force:
        fail(f"{target} already exists. Use --force to overwrite.")
        return 1
    profile = args.profile
    if profile not in PROFILES:
        fail(f"Unknown profile: {profile}")
        return 2
    region = args.region
    # Reuse-all-or-nothing: we never carve a subnet inside someone else's VPC.
    provided = sum(bool(x) for x in (args.vpc, args.subnet, args.security_group))
    if provided not in (0, 3):
        fail("--vpc, --subnet and --security-group must be given all together "
             "(reuse existing networking) or not at all (provision temporary ones)")
        return 2
    ingress_cidr = ""
    if provided == 0:
        raw_cidr = str(getattr(args, "ingress_cidr", "")).strip()
        if not raw_cidr:
            fail("--ingress-cidr is required when quickstart creates a security group "
                 "(use your runner's public IPv4 address with /32)")
            return 2
        try:
            ingress_cidr = _normalize_ingress_cidr(raw_cidr)
        except ConfigError as exc:
            fail(str(exc))
            return 2
    try:
        sid, key, tok = _creds()
    except ConfigError as exc:
        fail(str(exc))
        return 2

    banner("quickstart")
    try:
        zone = args.zone or _pick_zone(region, sid, key, tok)
        source = _pick_source_image(region, profile)
        instance = args.instance_type or _pick_instance_type(region, zone, profile)
    except ConfigError as exc:
        fail(str(exc))
        return 2

    dry_run = bool(getattr(args, "dry_run", False))
    vpc, subnet, sg = args.vpc, args.subnet, args.security_group
    created: dict[str, str] = {}
    if not dry_run and not (vpc and subnet and sg):
        try:
            if not vpc:
                vpc = _create_vpc(region, sid, key, tok)
                created["vpc_id"] = vpc
                _record_resources(target, region, created)
                info(f"Created temporary VPC: {vpc}")
            if not subnet:
                subnet = _create_subnet(region, zone, vpc, sid, key, tok)
                created["subnet_id"] = subnet
                _record_resources(target, region, created)
                info(f"Created temporary subnet: {subnet}")
            if not sg:
                sg = _create_security_group(region, sid, key, tok)
                created["security_group_id"] = sg
                _record_resources(target, region, created)
                _configure_security_group(region, sg, profile, ingress_cidr,
                                          sid, key, tok)
                info(f"Created temporary security group: {sg}")
        except ConfigError as exc:
            fail(f"Provisioning failed: {exc}")
            if created:
                warn("Rolling back resources created by this quickstart")
                if _cmd_cleanup(target) != 0:
                    warn(f"Rollback incomplete; retry: ohbs-image quickstart --target "
                         f"{target} --cleanup")
            return 1

    info(f"Zone:            {zone}")
    info(f"Source image:    {source}")
    info(f"Instance type:   {instance}")
    if dry_run:
        warn("dry-run: nothing was created (re-run without --dry-run to provision)")
        info(f"Next: ohbs-image quickstart --region {region} "
             f"--profile {profile} --zone {zone}")
        return 0

    content = _render_config_toml(
        profile=profile, region=region, zone=zone, instance=instance,
        source=source, vpc=vpc, subnet=subnet, sg=sg,
        public_ip=True, level=int(args.level), generated_by="quickstart")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    ok(f"Generated: {target}")
    if created:
        _record_resources(target, region, created)
        ok(f"Temporary resources recorded: {_resource_path(target)} "
           "(remove later with: ohbs-image quickstart --cleanup)")

    rc = ohbs_image.main(["doctor", "--config", str(target)])
    if rc != 0:
        fail("doctor reported problems — fix them and re-run quickstart")
        return rc
    rc = ohbs_image.main(["plan", "--config", str(target)])
    if rc != 0:
        fail("plan failed")
        return rc
    if getattr(args, "yes", False):
        ok("--yes: starting build")
        return ohbs_image.main(["build", "--config", str(target), "--yes"])
    info(f"Next: ohbs-image build --config {target}")
    return 0
