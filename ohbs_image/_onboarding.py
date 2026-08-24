from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import ohbs_image

from ._config import ResolvedConfig, load_config, resolve
from ._discover import discover_resources
from ._logging import ConfigError, banner, fail, info, ok, warn
from ._profiles import PROFILES


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    status: str
    summary: str
    detail: str = ""
    fix: str = ""


def _version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (result.stdout or result.stderr).strip().splitlines()
    return text[0] if text else Path(command[0]).name


def _numeric_version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(value or 0) for value in match.groups()) if match else ()


def collect_doctor_checks(config_path: str, *, cloud: bool = True) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    python_version = tuple(sys.version_info[:3])
    python_ok = (3, 11) <= python_version < (3, 15)
    checks.append(DoctorCheck(
        "python", "pass" if python_ok else "fail", f"Python {platform.python_version()}",
        sys.executable, "Install a supported Python 3.11–3.14 release" if not python_ok else ""))

    packer = shutil.which("packer")
    packer_version = _version([packer, "version"]) if packer else ""
    packer_ok = bool(packer and _numeric_version(packer_version) >= (1, 12, 0))
    checks.append(DoctorCheck(
        "packer", "pass" if packer_ok else "fail",
        packer_version if packer else "Packer is not installed",
        packer or "", "Install Packer 1.12+ from https://developer.hashicorp.com/packer/install"
        if not packer_ok else ""))

    ansible = shutil.which("ansible-playbook")
    ansible_version = _version([ansible, "--version"]) if ansible else ""
    ansible_ok = bool(ansible and _numeric_version(ansible_version) >= (2, 15, 0))
    checks.append(DoctorCheck(
        "ansible", "pass" if ansible_ok else "warn",
        ansible_version if ansible else "ansible-playbook is not installed",
        ansible or "", "Install ansible-core>=2.15 (required for Windows builds)"
        if not ansible_ok else ""))

    path = Path(config_path)
    if not path.exists():
        checks.append(DoctorCheck("config", "fail", f"Configuration not found: {path}",
                                  fix="Run: ohbs-image configure"))
        return checks
    try:
        r = resolve(load_config(path))
        checks.append(DoctorCheck("config", "pass", "Configuration is valid", str(path.resolve())))
    except ConfigError as exc:
        checks.append(DoctorCheck("config", "fail", "Configuration is invalid", str(exc),
                                  "Fix the reported field or run: ohbs-image configure --force"))
        return checks

    for env_name in (r.secret_id_env, r.secret_key_env):
        present = bool(os.environ.get(env_name))
        checks.append(DoctorCheck(f"credential.{env_name}", "pass" if present else "fail",
                                  f"{env_name} is set" if present else f"{env_name} is not set",
                                  fix=f"export {env_name}=..." if not present else ""))

    role = Path(__file__).parent / "roles" / r.role_dir
    checks.append(DoctorCheck("role", "pass" if role.is_dir() else "fail",
                              f"Bundled role {r.role_dir} is ready" if role.is_dir()
                              else f"Bundled role {r.role_dir} is missing",
                              str(role), "Reinstall ohbs-image" if not role.is_dir() else ""))

    if r.family == "windows":
        collection = ohbs_image._check_ansible_windows_collection()
        checks.append(DoctorCheck("ansible.windows", "pass" if collection else "fail",
                                  "ansible.windows collection is installed" if collection
                                  else "ansible.windows collection is missing",
                                  fix="ansible-galaxy collection install ansible.windows" if not collection else ""))
        winrm = ohbs_image._check_pywinrm()
        checks.append(DoctorCheck("pywinrm", "pass" if winrm else "fail",
                                  "pywinrm is importable" if winrm else "pywinrm is not installed",
                                  fix="pip install pywinrm" if not winrm else ""))

    creds_ready = bool(os.environ.get(r.secret_id_env) and os.environ.get(r.secret_key_env))
    if cloud and creds_ready:
        try:
            sid, skey, token = ohbs_image._creds(
                r.secret_id_env, r.secret_key_env, r.security_token_env)
            response = ohbs_image._tc3_api("cvm", "DescribeImages", "2017-03-12", r.region,
                                           {"ImageIds": [r.source_image_id], "Limit": 1},
                                           sid, skey, token)
            images = response.get("Response", {}).get("ImageSet", [])
            found = isinstance(images, list) and bool(images)
            checks.append(DoctorCheck("cloud.source_image", "pass" if found else "fail",
                                      f"Source image {r.source_image_id} is accessible" if found
                                      else f"Source image {r.source_image_id} was not found in {r.region}",
                                      fix="Choose a source image available in the configured region" if not found else ""))
            subnet_response = ohbs_image._tc3_api(
                "vpc", "DescribeSubnets", "2017-03-12", r.region,
                {"SubnetIds": [r.subnet_id]}, sid, skey, token)
            subnets = subnet_response.get("Response", {}).get("SubnetSet", [])
            subnet = subnets[0] if isinstance(subnets, list) and subnets else {}
            subnet_found = isinstance(subnet, dict) and bool(subnet)
            checks.append(DoctorCheck(
                "cloud.subnet", "pass" if subnet_found else "fail",
                f"Subnet {r.subnet_id} is accessible" if subnet_found else f"Subnet {r.subnet_id} was not found",
                fix="Select a subnet in the configured region" if not subnet_found else ""))
            if subnet_found:
                same_vpc = subnet.get("VpcId") == r.vpc_id
                checks.append(DoctorCheck(
                    "cloud.subnet_vpc", "pass" if same_vpc else "fail",
                    "Subnet belongs to the configured VPC" if same_vpc
                    else f"Subnet belongs to {subnet.get('VpcId')}, not {r.vpc_id}",
                    fix="Use a subnet and VPC from the same network" if not same_vpc else ""))
                same_zone = subnet.get("Zone") == r.zone
                checks.append(DoctorCheck(
                    "cloud.subnet_zone", "pass" if same_zone else "fail",
                    "Subnet belongs to the configured zone" if same_zone
                    else f"Subnet belongs to {subnet.get('Zone')}, not {r.zone}",
                    fix="Use a subnet in the configured build zone" if not same_zone else ""))
            sg_response = ohbs_image._tc3_api(
                "vpc", "DescribeSecurityGroups", "2017-03-12", r.region,
                {"SecurityGroupIds": [r.security_group_id]}, sid, skey, token)
            groups = sg_response.get("Response", {}).get("SecurityGroupSet", [])
            sg_found = isinstance(groups, list) and bool(groups)
            checks.append(DoctorCheck(
                "cloud.security_group", "pass" if sg_found else "fail",
                f"Security group {r.security_group_id} is accessible" if sg_found
                else f"Security group {r.security_group_id} was not found",
                fix="Select a security group in the configured region" if not sg_found else ""))
            checks.append(DoctorCheck(
                "cloud.write_permissions", "skip",
                "Mutating CAM permissions were not probed by doctor",
                "Required build permissions are validated by preflight/validate and the protected Canary.",
                "Grant the documented least-privilege build role before running build"))
            checks.append(DoctorCheck(
                "cloud.quotas", "skip",
                "Cloud quotas were not mutated or reserved by doctor",
                "Confirm CVM, image, disk and public-IP quotas before large matrix runs."))
        except Exception as exc:
            checks.append(DoctorCheck("cloud.api", "fail", "Tencent Cloud API check failed", str(exc),
                                      "Verify credentials, STS token, region, clock and network access"))
    elif cloud:
        checks.append(DoctorCheck("cloud.api", "skip", "Cloud checks skipped because credentials are missing"))
    else:
        checks.append(DoctorCheck("cloud.api", "skip", "Cloud checks disabled by --no-cloud"))
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = collect_doctor_checks(args.config, cloud=not args.no_cloud)
    blocked = any(check.status == "fail" for check in checks)
    if args.output == "json":
        print(json.dumps({"schema": "https://ohbs-image.dev/doctor/v1",
                          "ready": not blocked, "checks": [asdict(c) for c in checks]},
                         ensure_ascii=False, indent=2))
    else:
        banner("doctor")
        marks = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}
        for check in checks:
            line = f"[{marks[check.status]}] {check.summary}"
            (ok if check.status == "pass" else fail if check.status == "fail" else warn if check.status == "warn" else info)(line)
            if check.detail:
                info(f"       {check.detail}")
            if check.fix:
                info(f"       Fix: {check.fix}")
        info(f"Result: {'ready to build' if not blocked else 'blocked — fix failed checks first'}")
    return 1 if blocked else 0


def _ask(value: str | None, prompt: str, default: str = "") -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        raise ConfigError(f"{prompt} is required in non-interactive mode")
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _toml_value(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(ch in cleaned for ch in ('"', "\n", "\r", "\x00")):
        raise ConfigError(f"{label} contains characters that cannot be written safely to TOML")
    return cleaned


def _choose_resource(kind: str, region: str, *, zone: str = "", profile: str = "",
                     vpc_id: str = "") -> str:
    rows = discover_resources(kind, region, zone=zone, profile=profile, vpc_id=vpc_id)
    if not rows:
        raise ConfigError(f"No matching {kind} found in {region}")
    if not sys.stdin.isatty():
        if len(rows) == 1:
            return str(rows[0]["id"])
        raise ConfigError(f"Discovery found {len(rows)} {kind}; specify the ID in non-interactive mode")
    for index, row in enumerate(rows, 1):
        print(f"  {index}. {row.get('id')}  {row.get('name', '')}  {row.get('zone', '')}")
    raw = input(f"Select {kind} [1]: ").strip() or "1"
    try:
        return str(rows[int(raw) - 1]["id"])
    except (ValueError, IndexError):
        raise ConfigError(f"Invalid {kind} selection: {raw}") from None


def cmd_configure(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser()
    if target.exists() and not args.force:
        fail(f"{target} already exists. Use --force to overwrite.")
        return 1
    try:
        profile = _ask(args.profile, "Profile", "tencentos3")
        if profile not in PROFILES:
            raise ConfigError(f"Unknown profile: {profile}")
        region = _ask(args.region, "Region", "ap-guangzhou")
        zone = _ask(args.zone, "Zone")
        source = (args.source_image or _choose_resource("images", region, zone=zone, profile=profile)
                  if args.discover else _ask(args.source_image, "Source image ID"))
        vpc = (args.vpc or _choose_resource("vpcs", region)
               if args.discover else _ask(args.vpc, "VPC ID"))
        subnet = (args.subnet or _choose_resource("subnets", region, zone=zone, vpc_id=vpc)
                  if args.discover else _ask(args.subnet, "Subnet ID"))
        sg = (args.security_group or _choose_resource("security-groups", region)
              if args.discover else _ask(args.security_group, "Security group ID"))
        instance = args.instance_type or ("S5.LARGE4" if PROFILES[profile].get("family") == "windows" else "S5.MEDIUM2")
        profile = _toml_value(profile, "profile")
        region = _toml_value(region, "region")
        zone = _toml_value(zone, "zone")
        source = _toml_value(source, "source image")
        vpc = _toml_value(vpc, "VPC")
        subnet = _toml_value(subnet, "subnet")
        sg = _toml_value(sg, "security group")
        instance = _toml_value(instance, "instance type")
    except ConfigError as exc:
        fail(str(exc))
        return 2
    content = f'''# Generated by ohbs-image configure
[build]
profile = "{profile}"
region = "{region}"
zone = "{zone}"
instance_type = "{instance}"
source_image_id = "{source}"
vpc_id = "{vpc}"
subnet_id = "{subnet}"
security_group_id = "{sg}"
associate_public_ip = {str(bool(args.public_ip)).lower()}

[image]
name_prefix = "{profile}-cis"
copy_regions = []

[ohbs]
level = {args.level}

[cloud]
secret_id_env = "TENCENTCLOUD_SECRET_ID"
secret_key_env = "TENCENTCLOUD_SECRET_KEY"
{('winrm_password_env = "WINRM_PASSWORD"' if PROFILES[profile].get('family') == 'windows' else '')}

[meta]
os_tag = "{profile}"
benchmark = "{PROFILES[profile].get('benchmark', '')}"
'''
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    banner("configure")
    ok(f"Generated: {target}")
    info(f"Next: ohbs-image doctor --config {target}")
    return 0


def _plan_doc(r: ResolvedConfig) -> dict[str, Any]:
    return {
        "schema": "https://ohbs-image.dev/plan/v1", "mutates_cloud": False,
        "profile": r.profile_name, "family": r.family or "linux", "cis_level": r.level,
        "placement": {"region": r.region, "zone": r.zone, "vpc_id": r.vpc_id,
                      "subnet_id": r.subnet_id, "security_group_id": r.security_group_id},
        "source_image_id": r.source_image_id,
        "temporary_resources": [{"type": "CVM", "count": 1, "instance_type": r.instance_type,
                                 "lifecycle": "terminated after build"}],
        "outputs": ["custom image", "audit report", "lineage record", "release evidence"],
        "gates": {"minimum_score": r.min_score, "smoke_test": r.smoke_test,
                  "clean_boot_verify": r.verify_boot, "attestation_required": r.attestation_required},
        "distribution": {"copy_regions": r.image_copy_regions, "share_accounts": r.image_share_accounts},
        "limits": {"maximum_minutes": r.max_build_minutes},
        "cost": {"status": "provider-price-not-queried", "note": "CVM and image storage charges may apply"},
    }


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        r = resolve(load_config(Path(args.config)))
    except ConfigError as exc:
        fail(str(exc))
        return 2
    doc = _plan_doc(r)
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    banner("plan — read only")
    info(f"Build: {r.profile_name} CIS L{r.level} in {r.zone} ({r.region})")
    info(f"Source: {r.source_image_id} -> {r.image_name_prefix}-<timestamp>")
    info(f"Temporary: 1 × {r.instance_type} CVM; terminated after build")
    info(f"Maximum duration: {r.max_build_minutes} minutes")
    info(f"Gates: score >= {r.min_score}%, smoke={r.smoke_test}, clean-boot={r.verify_boot}")
    info(f"Distribution: {len(r.image_copy_regions)} copy region(s), {len(r.image_share_accounts)} account(s)")
    warn("Cost: live provider pricing is not queried; CVM and image storage charges may apply")
    ok("No cloud resources were created or modified")
    return 0
