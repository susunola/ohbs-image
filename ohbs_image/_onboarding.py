from __future__ import annotations

import argparse
import email.utils
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import ohbs_image

from ._config import ResolvedConfig, load_config, resolve
from ._discover import discover_resources
from ._logging import ConfigError, banner, fail, info, ok, warn
from ._profiles import PROFILES

# Doctor diagnostic groups (also the `--only` filter choices).
DOCTOR_GROUPS = ("toolchain", "config", "credentials", "cloud", "network", "permissions")

# Stable exit codes (see `docs/doctor.md` for the full contract).
EXIT_READY = 0        # no failing checks — ready to build
EXIT_BLOCKED = 1      # at least one failing check
EXIT_CONFIG = 2       # configuration could not be resolved

# Secret patterns redacted from every summary/detail/fix and saved report.
_SECRET_PATTERNS = (
    re.compile(r"AKID[0-9A-Za-z]{16,}"),
    re.compile(r"(?i)\b(secret_id|secret_key|access_key|security_token|token|password|winrm_password|appsecret)\b[\"']?\s*[=:]\s*[\"']?[0-9A-Za-z+/=_-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def _redact(text: str) -> str:
    """Replace credential-shaped substrings with '***' (best-effort)."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("***", redacted)
    return redacted


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    status: str
    summary: str
    detail: str = ""
    fix: str = ""
    group: str = "cloud"


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


def _packer_plugin_versions(packer: str | None) -> dict[str, str]:
    """Parse `packer plugins installed` for tencentcloud/ansible versions."""
    if not packer:
        return {}
    try:
        result = subprocess.run([packer, "plugins", "installed"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    versions: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        for plugin in ("tencentcloud", "ansible"):
            if plugin in line:
                match = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", line)
                if match:
                    versions[plugin] = "v" + match.group(1)
    return versions


def _toolchain_checks() -> list[DoctorCheck]:
    """Local toolchain diagnostics: Python, Packer, Ansible, plugins, GPG,
    coscli and optional audit tools (21–30)."""
    checks: list[DoctorCheck] = []
    python_version = tuple(sys.version_info[:3])
    python_ok = (3, 11) <= python_version < (3, 15)
    checks.append(DoctorCheck(
        "python", "pass" if python_ok else "fail", f"Python {platform.python_version()}",
        sys.executable, "Install a supported Python 3.11–3.14 release" if not python_ok else "",
        group="toolchain"))

    packer = shutil.which("packer")
    packer_version = _version([packer, "version"]) if packer else ""
    packer_ok = bool(packer and _numeric_version(packer_version) >= (1, 12, 0))
    checks.append(DoctorCheck(
        "packer", "pass" if packer_ok else "fail",
        packer_version if packer else "Packer is not installed",
        packer or "", "Install Packer 1.12+ from https://developer.hashicorp.com/packer/install"
        if not packer_ok else "", group="toolchain"))

    ansible = shutil.which("ansible-playbook")
    ansible_version = _version([ansible, "--version"]) if ansible else ""
    ansible_ok = bool(ansible and _numeric_version(ansible_version) >= (2, 15, 0))
    checks.append(DoctorCheck(
        "ansible", "pass" if ansible_ok else "warn",
        ansible_version if ansible else "ansible-playbook is not installed",
        ansible or "", "Install ansible-core>=2.15 (required for Windows builds)"
        if not ansible_ok else "", group="toolchain"))

    # 24/25 — Packer plugin versions (best-effort; Packer auto-installs at build).
    plugin_versions = _packer_plugin_versions(packer)
    for plugin in ("tencentcloud", "ansible"):
        version = plugin_versions.get(plugin, "")
        if version:
            checks.append(DoctorCheck(
                f"plugin.{plugin}", "pass", f"Packer {plugin} plugin {version}",
                group="toolchain"))
        elif packer:
            checks.append(DoctorCheck(
                f"plugin.{plugin}", "warn", f"Packer {plugin} plugin is not installed",
                "Packer installs missing plugins automatically during validate/build",
                f"Run: packer plugins install github.com/hashicorp/packer-plugin-{plugin}",
                group="toolchain"))
        else:
            checks.append(DoctorCheck(
                f"plugin.{plugin}", "skip", f"Packer {plugin} plugin version unknown (Packer missing)",
                group="toolchain"))

    # 28 — GPG for provenance signing/verification.
    gpg = shutil.which("gpg")
    checks.append(DoctorCheck(
        "gpg", "pass" if gpg else "warn",
        "GPG is available" if gpg else "GPG is not installed",
        gpg or "", "Install gnupg to sign/verify SLSA provenance" if not gpg else "",
        group="toolchain"))

    # 29 — coscli for the COS state backend.
    coscli = shutil.which("coscli")
    checks.append(DoctorCheck(
        "coscli", "pass" if coscli else "warn",
        (_version([coscli, "--version"]) or "coscli is available") if coscli else "coscli is not installed",
        coscli or "", "Install coscli for the COS state backend" if not coscli else "",
        group="toolchain"))

    # 30 — optional third-party audit tools (never blocking).
    for tool, label, version_args in (
        ("trivy", "Trivy", ["--version"]),
        ("oscap", "OpenSCAP", ["--version"]),
        ("inspec", "InSpec", ["version"]),
    ):
        path = shutil.which(tool)
        if path:
            checks.append(DoctorCheck(
                f"tool.{tool}", "pass", f"{label} is available",
                _version([path, *version_args]), group="toolchain"))
        else:
            checks.append(DoctorCheck(
                f"tool.{tool}", "info", f"{label} is not installed (optional)",
                "Only needed for independent 'audit' runs", group="toolchain"))
    checks.append(DoctorCheck(
        "tool.hardeningkitty", "info",
        "HardeningKitty is only checked on Windows audit hosts",
        "PowerShell module; used by 'audit --auditor kitty'", group="toolchain"))
    return checks


def _config_checks(r: ResolvedConfig) -> list[DoctorCheck]:
    """Bundled role and Windows runtime readiness (part of the config group)."""
    checks: list[DoctorCheck] = []
    role = Path(__file__).parent / "roles" / r.role_dir
    checks.append(DoctorCheck(
        "role", "pass" if role.is_dir() else "fail",
        f"Bundled role {r.role_dir} is ready" if role.is_dir()
        else f"Bundled role {r.role_dir} is missing",
        str(role), "Reinstall ohbs-image" if not role.is_dir() else "", group="config"))
    if r.family == "windows":
        collection = ohbs_image._check_ansible_windows_collection()
        checks.append(DoctorCheck(
            "ansible.windows", "pass" if collection else "fail",
            "ansible.windows collection is installed" if collection
            else "ansible.windows collection is missing",
            fix="ansible-galaxy collection install ansible.windows" if not collection else "",
            group="config"))
        winrm = ohbs_image._check_pywinrm()
        checks.append(DoctorCheck(
            "pywinrm", "pass" if winrm else "fail",
            "pywinrm is importable" if winrm else "pywinrm is not installed",
            fix="pip install pywinrm" if not winrm else "", group="config"))
    return checks


def _safe_tc(service: str, action: str, version: str, params: dict[str, Any],
             r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> dict[str, Any] | None:
    """Call a Tencent Cloud read API, returning None on any failure so a single
    broken endpoint degrades to a skip instead of failing the whole doctor run."""
    try:
        return ohbs_image._tc3_api(service, action, version, r.region, params, sid, skey, tok)
    except Exception:
        return None


def _server_clock_offset() -> float | None:
    """Seconds between the local clock and Tencent's edge clock (None on failure).

    TC3-HMAC-SHA256 signing is rejected when the local clock is more than
    ~5 minutes from Tencent Cloud time, so doctor surfaces the skew early.
    """
    try:
        with urllib.request.urlopen("https://cvm.tencentcloudapi.com/", timeout=5) as resp:  # noqa: S310
            date = resp.headers.get("Date")
            if not date:
                return None
            server = email.utils.parsedate_to_datetime(date)
            return abs(server.timestamp() - time.time())
    except Exception:
        return None


def _os_matches(os_name: str, os_tag: str) -> bool:
    """Loose match of a DescribeImages OsName against a profile os_tag
    (e.g. 'TencentOS Server 3.1 (Final)' vs 'tencentos-3')."""
    name = os_name.lower()
    family, sep, version = os_tag.lower().partition("-")
    if not family:
        return False
    return family in name and (not sep or version in name)


def _credentials_checks(r: ResolvedConfig, *, cloud: bool, offline: bool) -> list[DoctorCheck]:
    """Credential presence, validity, STS expiry and clock skew (31–33)."""
    checks: list[DoctorCheck] = []
    for env_name in (r.secret_id_env, r.secret_key_env):
        present = bool(os.environ.get(env_name))
        checks.append(DoctorCheck(
            f"credential.{env_name}", "pass" if present else "fail",
            f"{env_name} is set" if present else f"{env_name} is not set",
            fix=f"export {env_name}=..." if not present else "", group="credentials"))
    sid, skey, tok = ohbs_image._creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
    have_creds = bool(sid and skey)
    if not cloud or offline or not have_creds:
        checks.append(DoctorCheck(
            "credential.valid", "skip",
            "Credential validity not verified (offline or missing credentials)" if have_creds
            else "Credential validity not verified (credentials missing)",
            group="credentials"))
        checks.append(DoctorCheck(
            "sts.expiry", "skip", "STS token expiry not checked (no token or offline)",
            group="credentials"))
    else:
        # 31 — verify the credentials actually authenticate.
        identity = _safe_tc("sts", "GetCallerIdentity", "2018-08-13", {}, r, sid, skey, tok)
        if identity is None:
            checks.append(DoctorCheck(
                "credential.valid", "fail",
                "Credentials could not be validated via sts.GetCallerIdentity",
                "Check the AK/SK values and that the account allows sts access",
                "Run: tccli sts GetCallerIdentity", group="credentials"))
        else:
            account = str(identity.get("Response", {}).get("AccountId", ""))
            checks.append(DoctorCheck(
                "credential.valid", "pass", "Credentials are valid",
                f"Account {_redact(account)}", group="credentials"))
        # 32 — STS session expiry.
        if tok:
            checks.append(DoctorCheck(
                "sts.expiry", "warn", "STS session token in use",
                "Remaining validity cannot be derived from the environment; record ExpiredTime where the token is minted",
                "Re-issue credentials before expiry in long-running pipelines",
                group="credentials"))
        else:
            checks.append(DoctorCheck(
                "sts.expiry", "pass", "Long-lived AK/SK in use (no STS expiry)",
                group="credentials"))
    # 33 — local clock vs Tencent edge clock.
    offset = _server_clock_offset() if not offline else None
    if offset is None:
        checks.append(DoctorCheck(
            "clock", "info" if cloud and not offline else "skip",
            "Clock skew not verified" if offline else "Clock skew check unavailable",
            "TC3 signatures require the local clock within 5 minutes of Tencent Cloud time",
            group="credentials"))
    elif offset > 300:
        checks.append(DoctorCheck(
            "clock", "fail", f"Local clock is {offset:.0f}s off Tencent Cloud time",
            "TC3 signing will be rejected",
            "Sync the clock (systemd-timesyncd / ntp)", group="credentials"))
    elif offset > 60:
        checks.append(DoctorCheck(
            "clock", "warn", f"Local clock is {offset:.0f}s off Tencent Cloud time",
            "Large skew may break TC3 signing", group="credentials"))
    else:
        checks.append(DoctorCheck(
            "clock", "pass", f"Local clock is within {offset:.0f}s of Tencent Cloud time",
            group="credentials"))
    return checks


def _check_cloud_region_zone(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Region existence and zone-in-region membership (34–35)."""
    checks: list[DoctorCheck] = []
    resp = _safe_tc("cvm", "DescribeRegions", "2017-03-12", {}, r, sid, skey, tok)
    if resp is None:
        checks.append(DoctorCheck("cloud.region", "skip", "Region check unavailable (API error)", group="cloud"))
    else:
        regions = {str(x.get("Region")) for x in resp.get("Response", {}).get("RegionSet", [])}
        found = r.region in regions
        checks.append(DoctorCheck(
            "cloud.region", "pass" if found else "fail",
            f"Region {r.region} exists" if found else f"Region {r.region} was not found",
            fix="Pick a valid region (ohbs-image discover vpcs)" if not found else "", group="cloud"))
    resp = _safe_tc("cvm", "DescribeZones", "2017-03-12", {}, r, sid, skey, tok)
    if resp is None:
        checks.append(DoctorCheck("cloud.zone", "skip", "Zone check unavailable (API error)", group="cloud"))
    else:
        zones = {str(x.get("Zone")) for x in resp.get("Response", {}).get("ZoneSet", [])}
        found = r.zone in zones
        checks.append(DoctorCheck(
            "cloud.zone", "pass" if found else "fail",
            f"Zone {r.zone} exists in {r.region}" if found else f"Zone {r.zone} is not in {r.region}",
            fix="Pick a zone listed by DescribeZones" if not found else "", group="cloud"))
    return checks


def _check_cloud_image(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Source image: existence, OS/profile match and architecture (36–38)."""
    checks: list[DoctorCheck] = []
    resp = _safe_tc("cvm", "DescribeImages", "2017-03-12",
                    {"ImageIds": [r.source_image_id], "Limit": 1}, r, sid, skey, tok)
    if resp is None:
        checks.append(DoctorCheck(
            "cloud.source_image", "skip", "Source image check unavailable (API error)", group="cloud"))
        return checks
    images = resp.get("Response", {}).get("ImageSet", [])
    if not isinstance(images, list) or not images:
        checks.append(DoctorCheck(
            "cloud.source_image", "fail",
            f"Source image {r.source_image_id} was not found in {r.region}",
            fix="Choose a source image available in the configured region", group="cloud"))
        return checks
    image = images[0]
    checks.append(DoctorCheck(
        "cloud.source_image", "pass", f"Source image {r.source_image_id} is accessible",
        group="cloud"))
    os_name = str(image.get("OsName") or "")
    profile_os = str(PROFILES.get(r.profile_name, {}).get("os_tag", ""))
    if os_name and profile_os and _os_matches(os_name, profile_os):
        checks.append(DoctorCheck(
            "cloud.image_os", "pass", f"Source image OS matches profile ({os_name})",
            group="cloud"))
    elif os_name:
        checks.append(DoctorCheck(
            "cloud.image_os", "warn",
            f"Source image OS '{os_name}' may not match profile '{r.profile_name}'",
            "Custom images may carry generic names; confirm manually before building",
            group="cloud"))
    else:
        checks.append(DoctorCheck(
            "cloud.image_os", "skip", "Source image OS unknown (no OsName reported)",
            group="cloud"))
    arch = str(image.get("Architecture") or "")
    if arch:
        checks.append(DoctorCheck(
            "cloud.image_arch", "info", f"Source image architecture: {arch}",
            group="cloud"))
    else:
        checks.append(DoctorCheck(
            "cloud.image_arch", "skip", "Source image architecture unknown", group="cloud"))
    return checks


def _check_cloud_network(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Subnet/VPC/zone relationships (41–43)."""
    checks: list[DoctorCheck] = []
    resp = _safe_tc("vpc", "DescribeSubnets", "2017-03-12",
                    {"SubnetIds": [r.subnet_id]}, r, sid, skey, tok)
    if resp is None:
        checks.append(DoctorCheck("cloud.subnet", "skip", "Subnet check unavailable (API error)", group="cloud"))
        return checks
    subnets = resp.get("Response", {}).get("SubnetSet", [])
    subnet = subnets[0] if isinstance(subnets, list) and subnets else {}
    subnet_found = isinstance(subnet, dict) and bool(subnet)
    checks.append(DoctorCheck(
        "cloud.subnet", "pass" if subnet_found else "fail",
        f"Subnet {r.subnet_id} is accessible" if subnet_found else f"Subnet {r.subnet_id} was not found",
        fix="Select a subnet in the configured region" if not subnet_found else "", group="cloud"))
    if subnet_found:
        same_vpc = subnet.get("VpcId") == r.vpc_id
        checks.append(DoctorCheck(
            "cloud.subnet_vpc", "pass" if same_vpc else "fail",
            "Subnet belongs to the configured VPC" if same_vpc
            else f"Subnet belongs to {subnet.get('VpcId')}, not {r.vpc_id}",
            fix="Use a subnet and VPC from the same network" if not same_vpc else "", group="cloud"))
        same_zone = subnet.get("Zone") == r.zone
        checks.append(DoctorCheck(
            "cloud.subnet_zone", "pass" if same_zone else "fail",
            "Subnet belongs to the configured zone" if same_zone
            else f"Subnet belongs to {subnet.get('Zone')}, not {r.zone}",
            fix="Use a subnet in the configured build zone" if not same_zone else "", group="cloud"))
    return checks


def _check_cloud_security_group(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Security group existence (44)."""
    resp = _safe_tc("vpc", "DescribeSecurityGroups", "2017-03-12",
                    {"SecurityGroupIds": [r.security_group_id]}, r, sid, skey, tok)
    if resp is None:
        return [DoctorCheck("cloud.security_group", "skip",
                            "Security group check unavailable (API error)", group="cloud")]
    groups = resp.get("Response", {}).get("SecurityGroupSet", [])
    found = isinstance(groups, list) and bool(groups)
    return [DoctorCheck(
        "cloud.security_group", "pass" if found else "fail",
        f"Security group {r.security_group_id} is accessible" if found
        else f"Security group {r.security_group_id} was not found",
        fix="Select a security group in the configured region" if not found else "", group="cloud")]


def _check_cloud_instance(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Instance type purchasability/stock in the build zone (39–40)."""
    checks: list[DoctorCheck] = []
    resp = _safe_tc("cvm", "DescribeZoneInstanceConfigInfos", "2017-03-12",
                    {"Filters": [{"Name": "zone", "Values": [r.zone]}],
                     "InstanceChargeType": "POSTPAID_BY_HOUR"},
                    r, sid, skey, tok)
    if resp is None:
        checks.append(DoctorCheck("cloud.instance_type", "skip",
                                  "Instance type check unavailable (API error)", group="cloud"))
        checks.append(DoctorCheck("cloud.instance_stock", "skip",
                                  "Instance stock check unavailable (API error)", group="cloud"))
        return checks
    configs = resp.get("Response", {}).get("InstanceTypeQuotaSet", [])
    purchasable = {str(x.get("InstanceType")) for x in configs if isinstance(x, dict)}
    available = r.instance_type in purchasable
    checks.append(DoctorCheck(
        "cloud.instance_type", "pass" if available else "fail",
        f"Instance type {r.instance_type} is purchasable in {r.zone}" if available
        else f"Instance type {r.instance_type} is not purchasable in {r.zone}",
        fix="Run: ohbs-image discover instance-types --zone " + r.zone if not available else "",
        group="cloud"))
    checks.append(DoctorCheck(
        "cloud.instance_stock", "pass" if available else "fail",
        f"Instance type {r.instance_type} has stock in {r.zone}" if available
        else f"Instance type {r.instance_type} has no stock in {r.zone}",
        group="cloud"))
    return checks


def _check_cloud_ingress(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """SSH/WinRM inbound rules on the configured security group (45–46)."""
    checks: list[DoctorCheck] = []
    resp = _safe_tc("vpc", "DescribeSecurityGroupPolicies", "2017-03-12",
                    {"SecurityGroupId": r.security_group_id}, r, sid, skey, tok)
    if resp is None or "Error" in resp.get("Response", {}):
        checks.append(DoctorCheck(
            "network.ingress", "skip",
            "Security group policy check unavailable (API error)", group="network"))
        return checks
    policies = resp.get("Response", {}).get("SecurityGroupPolicySet") or {}
    my_ip = ohbs_image._my_public_ip()
    if not my_ip:
        checks.append(DoctorCheck(
            "network.ingress", "info",
            "Cannot determine this machine's public IP — exact ingress check skipped",
            "Build will fail if the security group blocks the build port from this IP",
            group="network"))
        return checks
    if r.family == "windows":
        targets = [(5985, "winrm-http"), (5986, "winrm-https")]
    else:
        targets = [(r.ssh_port or 22, "ssh")]
    verdicts: dict[str, bool | None] = {}
    for port, label in targets:
        verdicts[label] = ohbs_image._sg_ingress_allows(policies, my_ip, port)
    allowed = [label for label, v in verdicts.items() if v is True]
    blocked = [label for label, v in verdicts.items() if v is False]
    unknown = [label for label, v in verdicts.items() if v is None]
    if allowed:
        checks.append(DoctorCheck(
            "network.ingress", "pass",
            f"Inbound from {my_ip} is allowed ({', '.join(allowed)})", group="network"))
    elif blocked and not unknown:
        checks.append(DoctorCheck(
            "network.ingress", "fail",
            f"Inbound from {my_ip} is blocked by the security group ({', '.join(blocked)})",
            f"Add an inbound rule {my_ip}/32 : TCP {', '.join(str(p) for p, _ in targets)} (ACCEPT)",
            group="network"))
    else:
        checks.append(DoctorCheck(
            "network.ingress", "warn",
            "Inbound policy references templates — cannot verify locally",
            "Confirm the security group allows the build host before running build",
            group="network"))
    return checks


def _check_cloud_egress(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Private-network egress and software-repo reachability (47–48)."""
    checks: list[DoctorCheck] = []
    resp = _safe_tc("vpc", "DescribeRouteTables", "2017-03-12",
                    {"Filters": [{"Name": "vpc-id", "Values": [r.vpc_id]}]},
                    r, sid, skey, tok)
    if resp is None:
        checks.append(DoctorCheck("network.egress", "skip",
                                  "Route table check unavailable (API error)", group="network"))
        checks.append(DoctorCheck("network.repo", "skip",
                                  "Software-repo reachability not evaluated (route check unavailable)",
                                  group="network"))
        return checks
    default_route = False
    for table in resp.get("Response", {}).get("RouteTableSet", []) or []:
        for route in table.get("RouteSet", []) or []:
            if route.get("DestinationCidrBlock") == "0.0.0.0/0":
                default_route = True
    if default_route:
        checks.append(DoctorCheck(
            "network.egress", "pass",
            "VPC has a default 0.0.0.0/0 route (public egress available)", group="network"))
        checks.append(DoctorCheck(
            "network.repo", "info",
            "Outbound software repos are expected to be reachable during build",
            "Private builds should point [build.packer] at internal mirrors", group="network"))
    else:
        checks.append(DoctorCheck(
            "network.egress", "warn",
            "VPC has no 0.0.0.0/0 route — private-network builds cannot reach public software repos",
            "Attach a NAT gateway or configure internal mirrors", group="network"))
        checks.append(DoctorCheck(
            "network.repo", "warn",
            "Private-network build: configure an internal software mirror before building",
            group="network"))
    return checks


def _check_cloud_quotas(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """CVM/image/EIP quota probes (50). Best-effort — API failures degrade to skip."""
    checks: list[DoctorCheck] = []
    probes: list[tuple[str, str, str, dict[str, Any], str]] = [
        ("cvm", "DescribeAccountQuota", "2017-03-12", {}, "CVM"),
        ("cvm", "DescribeImageQuota", "2017-03-12", {}, "image"),
        ("vpc", "DescribeAddressQuota", "2017-03-12", {}, "EIP"),
    ]
    reached = 0
    low: list[str] = []
    for service, action, version, params, _label in probes:
        resp = _safe_tc(service, action, version, params, r, sid, skey, tok)
        if resp is None:
            continue
        reached += 1
        body = resp.get("Response", {}) or {}
        if action == "DescribeAccountQuota":
            remaining: list[int] = []
            for item in body.get("PostPaidQuotaSet", []) or []:
                try:
                    remaining.append(int(item.get("TotalCount") or 0) - int(item.get("UsedCount") or 0))
                except (TypeError, ValueError):
                    continue
            if remaining and min(remaining) < 1:
                low.append("CVM post-paid quota exhausted")
        elif action == "DescribeImageQuota":
            quota = body.get("ImageNumQuota")
            try:
                if isinstance(quota, (int, float)) and int(quota) < 1:
                    low.append("image quota exhausted")
            except (TypeError, ValueError):
                pass
        elif action == "DescribeAddressQuota":
            for item in body.get("QuotaSet", []) or []:
                if str(item.get("QuotaName", "")).startswith("TOTAL_EIP"):
                    try:
                        if int(item.get("QuotaLimit") or 0) - int(item.get("QuotaCurrent") or 0) < 1:
                            low.append("EIP quota exhausted")
                    except (TypeError, ValueError):
                        pass
    if reached == 0:
        checks.append(DoctorCheck("cloud.quotas", "skip",
                                  "Quota checks unavailable (API errors)", group="cloud"))
    elif low:
        checks.append(DoctorCheck(
            "cloud.quotas", "warn", "Some cloud quotas are low: " + ", ".join(low),
            "Builds create one temporary CVM and one image; raise quotas before matrix runs",
            group="cloud"))
    else:
        checks.append(DoctorCheck(
            "cloud.quotas", "pass", "CVM/image/EIP quotas look sufficient", group="cloud"))
    return checks


def _cloud_checks(r: ResolvedConfig, sid: str, skey: str, tok: str | None) -> list[DoctorCheck]:
    """Every read-only Tencent Cloud diagnostic (34–50). Each probe is isolated
    so one unavailable endpoint degrades to a skip instead of failing the run."""
    checks: list[DoctorCheck] = []
    checks += _check_cloud_region_zone(r, sid, skey, tok)
    checks += _check_cloud_image(r, sid, skey, tok)
    checks += _check_cloud_network(r, sid, skey, tok)
    checks += _check_cloud_security_group(r, sid, skey, tok)
    checks += _check_cloud_instance(r, sid, skey, tok)
    checks += _check_cloud_ingress(r, sid, skey, tok)
    checks += _check_cloud_egress(r, sid, skey, tok)
    checks += _check_cloud_quotas(r, sid, skey, tok)
    # 49 — CAM permissions (behavioural probe: every read call above succeeded?).
    cloud_failed = [c for c in checks if c.status == "fail"]
    cloud_skipped = [c for c in checks if c.status == "skip"]
    if not cloud_failed and not cloud_skipped:
        checks.append(DoctorCheck(
            "permissions", "pass",
            "All read-only cloud probes passed — credentials can read the configured resources",
            "Least-privilege build policies: see docs/cam-permissions.md",
            group="permissions"))
    elif not cloud_failed:
        checks.append(DoctorCheck(
            "permissions", "skip",
            "Permission probes incomplete (some cloud checks unavailable)",
            "Least-privilege build policies: see docs/cam-permissions.md",
            group="permissions"))
    else:
        checks.append(DoctorCheck(
            "permissions", "fail",
            "Cloud probes failed — credentials may lack read access to the configured resources",
            "Least-privilege build policies: see docs/cam-permissions.md",
            group="permissions"))
    return checks


def collect_doctor_checks(config_path: str, *, cloud: bool = True, offline: bool = False,
                          only: str | None = None) -> list[DoctorCheck]:
    """Run every enabled doctor diagnostic and return the results.

    *cloud* toggles Tencent Cloud API checks (False = --no-cloud); *offline*
    additionally skips network probes such as the public-IP lookup and clock
    skew. *only* restricts the run to one diagnostic group (DOCTOR_GROUPS).

    Exit-code contract: EXIT_READY when no check fails, EXIT_BLOCKED when at
    least one check fails, EXIT_CONFIG when the configuration cannot be read.
    """
    checks: list[DoctorCheck] = _toolchain_checks()
    r: ResolvedConfig | None = None
    path = Path(config_path)
    if not path.exists():
        checks.append(DoctorCheck(
            "config", "fail", f"Configuration not found: {path}",
            fix="Run: ohbs-image configure", group="config"))
    else:
        try:
            r = resolve(load_config(path))
            checks.append(DoctorCheck(
                "config", "pass", "Configuration is valid", str(path.resolve()), group="config"))
        except ConfigError as exc:
            checks.append(DoctorCheck(
                "config", "fail", "Configuration is invalid", str(exc),
                "Fix the reported field or run: ohbs-image configure --force", group="config"))
    if r is not None:
        checks += _config_checks(r)
        checks += _credentials_checks(r, cloud=cloud, offline=offline)
        if cloud and not offline:
            sid, skey, tok = ohbs_image._creds(r.secret_id_env, r.secret_key_env, r.security_token_env)
            if sid and skey:
                checks += _cloud_checks(r, sid, skey, tok)
            else:
                checks.append(DoctorCheck(
                    "cloud.api", "skip",
                    "Cloud checks skipped because credentials are missing", group="cloud"))
        else:
            checks.append(DoctorCheck(
                "cloud.api", "skip",
                "Cloud checks disabled (--no-cloud or --offline)" if cloud else "Cloud checks disabled by --no-cloud",
                group="cloud"))
    else:
        checks.append(DoctorCheck(
            "cloud.api", "skip",
            "Cloud checks skipped because configuration is missing", group="cloud"))
    if only and only != "all":
        checks = [check for check in checks if check.group == only]
    return checks


_DOCTOR_MARKS = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP", "info": "INFO"}


def _sarif_report(checks: list[DoctorCheck], duration_ms: int) -> dict[str, Any]:
    """Build a SARIF 2.1.0 report (roadmap 59) from doctor checks."""
    level_map = {"fail": "error", "warn": "warning", "pass": "none", "skip": "note", "info": "note"}
    rules: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for check in checks:
        rule_id = f"ohbs/{check.id}"
        if not any(r["id"] == rule_id for r in rules):
            rules.append({
                "id": rule_id,
                "name": check.id,
                "shortDescription": {"text": check.summary},
                "help": {"text": check.fix or check.detail or check.summary},
                "properties": {"group": check.group},
            })
        results.append({
            "ruleId": rule_id,
            "level": level_map[check.status],
            "message": {"text": check.detail or check.summary},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": "ohbs-image"}}}],
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "ohbs-image doctor", "informationUri": "https://ohbs-image.dev/", "rules": rules}},
            "results": results,
            "invocations": [{"executionSuccessful": not any(c.status == "fail" for c in checks),
                             "properties": {"duration_ms": duration_ms, "redacted": True}}],
        }],
    }


def _render_text(checks: list[DoctorCheck], duration_ms: int) -> list[str]:
    """Render checks as human-readable lines (used by the text output)."""
    lines: list[str] = []
    for check in checks:
        mark = _DOCTOR_MARKS[check.status]
        lines.append(f"[{mark}] {check.summary}")
        if check.detail:
            lines.append(f"       {check.detail}")
        if check.fix:
            lines.append(f"       Fix: {check.fix}")
    blocked = any(check.status == "fail" for check in checks)
    lines.append(f"Result: {'ready to build' if not blocked else 'blocked — fix failed checks first'}"
                 f" ({len(checks)} checks in {duration_ms} ms)")
    return lines


def _write_report(path: str, checks: list[DoctorCheck], duration_ms: int, out_format: str) -> None:
    """Persist a redacted diagnostic report (roadmap 60)."""
    report_path = Path(path).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if out_format == "json":
        payload: Any = {"schema": "https://ohbs-image.dev/doctor/v1",
                        "ready": not any(c.status == "fail" for c in checks),
                        "diagnostics": {"duration_ms": duration_ms, "redacted": True},
                        "checks": [asdict(c) for c in checks]}
    elif out_format == "sarif":
        payload = _sarif_report(checks, duration_ms)
    else:
        payload = "\n".join(_render_text(checks, duration_ms))
    text = json.dumps(payload, ensure_ascii=False, indent=2) if isinstance(payload, dict) else payload
    report_path.write_text(_redact(text) + ("\n" if isinstance(payload, dict) else ""), encoding="utf-8")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run doctor diagnostics.

    Roadmap: 21-60 (doctor redesign). Renders 28 new checks grouped into
    toolchain/config/credentials/cloud/network/permissions, supports
    --only <group> (46), --offline (47), SARIF output (59), stable exit
    codes (58) and --report-path (60). All output is redacted (56).
    """
    only = getattr(args, "only", "all")
    offline = bool(getattr(args, "offline", False))
    report_path = getattr(args, "report_path", None)
    out_format = getattr(args, "output", "text")
    started = time.perf_counter()
    checks = collect_doctor_checks(args.config, cloud=not (args.no_cloud or offline),
                                   offline=offline, only=only)
    duration_ms = int((time.perf_counter() - started) * 1000)
    blocked = any(check.status == "fail" for check in checks)
    config_broken = any(check.id == "config" and check.status == "fail" for check in checks)
    exit_code = EXIT_CONFIG if config_broken else (EXIT_BLOCKED if blocked else EXIT_READY)
    if out_format == "json":
        print(json.dumps({"schema": "https://ohbs-image.dev/doctor/v1",
                          "ready": not blocked,
                          "diagnostics": {"duration_ms": duration_ms, "redacted": True,
                                          "exit_code": exit_code},
                          "checks": [asdict(check) for check in checks]},
                         ensure_ascii=False, indent=2))
    elif out_format == "sarif":
        print(json.dumps(_sarif_report(checks, duration_ms), ensure_ascii=False, indent=2))
    else:
        banner("doctor")
        for line in _render_text(checks, duration_ms):
            mark = line.split("]", 1)[0] + "]"
            if "[PASS]" in mark:
                ok(line)
            elif "[FAIL]" in mark:
                fail(line)
            elif "[WARN]" in mark:
                warn(line)
            else:
                info(line)
    if report_path:
        _write_report(report_path, checks, duration_ms, out_format)
        ok(f"Report saved: {report_path}")
    return exit_code


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
