from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

import ohbs_image

from ._logging import fail


def _credentials() -> tuple[str, str, str]:
    sid = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    key = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    token = os.environ.get("TENCENTCLOUD_SECURITY_TOKEN", "")
    if not sid or not key:
        raise OSError("TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY are required")
    return sid, key, token


def discover_resources(kind: str, region: str, *, zone: str = "",
                       profile: str = "", vpc_id: str = "",
                       min_cpu: int = 0, min_mem: int = 0,
                       in_stock: bool = False) -> list[dict[str, Any]]:
    sid, key, token = _credentials()
    if kind == "instance-types":
        if not zone:
            raise OSError("instance-types discovery requires --zone")
        it_params: dict[str, Any] = {
            "Filters": [{"Name": "zone", "Values": [zone]}],
        }
        raw = ohbs_image._tc3_api("cvm", "DescribeZoneInstanceConfigInfos", "2017-03-12",
                                  region, it_params, sid, key, token)
        rows = raw.get("Response", {}).get("InstanceTypeQuotaSet", []) or []
        result = []
        seen_types: set[str] = set()
        for r in rows if isinstance(rows, list) else []:
            # DescribeZoneInstanceConfigInfos rejects InstanceChargeType as a
            # request parameter. It returns one row per charge type instead,
            # so select postpaid rows locally and de-duplicate the display.
            charge_type = str(r.get("InstanceChargeType", ""))
            if charge_type and charge_type != "POSTPAID_BY_HOUR":
                continue
            instance_type = str(r.get("InstanceType", ""))
            if not instance_type or instance_type in seen_types:
                continue
            cpu = r.get("Cpu", 0) or 0
            mem = r.get("Memory", 0) or 0
            status = str(r.get("Status", "")).upper()
            if min_cpu and int(cpu) < min_cpu:
                continue
            if min_mem and float(mem) < min_mem:
                continue
            if in_stock and status in ("SOLD_OUT", "UNAVAILABLE"):
                continue
            seen_types.add(instance_type)
            result.append({
                "id": instance_type,
                "name": instance_type,
                "zone": zone,
                "cpu": int(cpu),
                "memory": int(mem),
                "gpu": int(r.get("GPU", 0) or 0),
                "status": status,
            })
        return result
    if kind == "images":
        # ImageType is a response field, not a DescribeImages request
        # parameter. Passing it returns an empty set without an API error.
        params: dict[str, Any] = {"Limit": 100}
        raw = ohbs_image._tc3_api("cvm", "DescribeImages", "2017-03-12", region,
                                  params, sid, key, token)
        rows = raw.get("Response", {}).get("ImageSet", [])
        result = []
        profile_needles = {
            "ubuntu2004": ("ubuntu", "20.04"), "ubuntu2204": ("ubuntu", "22.04"),
            "ubuntu2404": ("ubuntu", "24.04"), "rhel8": ("rhel", "8"),
            "rhel9": ("rhel", "9"), "rhel10": ("rhel", "10"),
            "win2016": ("windows", "2016"), "win2019": ("windows", "2019"),
            "win2022": ("windows", "2022"), "win2025": ("windows", "2025"),
        }
        needles = profile_needles.get(profile, ())
        for row in rows if isinstance(rows, list) else []:
            if row.get("ImageType") not in (None, "", "PUBLIC_IMAGE"):
                continue
            name = str(row.get("ImageName", ""))
            haystack = f"{name} {row.get('OsName', '')}".lower()
            if profile in ("tencentos3", "tencentos4"):
                major = profile[-1]
                # Match the OS major next to the product name. A generic
                # digit match confuses TencentOS 3.3/TK4 with TencentOS 4.
                if not re.search(rf"tencent\s*os(?:\s+server)?\s+{major}(?:\D|$)", haystack):
                    continue
            if needles and not all(needle in haystack for needle in needles):
                continue
            result.append({"id": row.get("ImageId", ""), "name": name,
                           "os": row.get("OsName", ""),
                           "architecture": row.get("Architecture", ""),
                           "state": row.get("ImageState", ""),
                           "created_at": row.get("CreatedTime", "")})
        return result
    if kind == "vpcs":
        raw = ohbs_image._tc3_api("vpc", "DescribeVpcs", "2017-03-12", region,
                                  {"Limit": "100"}, sid, key, token)
        rows = raw.get("Response", {}).get("VpcSet", [])
        return [{"id": r.get("VpcId", ""), "name": r.get("VpcName", ""),
                 "cidr": r.get("CidrBlock", "")} for r in rows if isinstance(r, dict)]
    if kind == "subnets":
        filters = []
        if zone:
            filters.append({"Name": "zone", "Values": [zone]})
        if vpc_id:
            filters.append({"Name": "vpc-id", "Values": [vpc_id]})
        raw = ohbs_image._tc3_api("vpc", "DescribeSubnets", "2017-03-12", region,
                                  {"Limit": "100", "Filters": filters}, sid, key, token)
        rows = raw.get("Response", {}).get("SubnetSet", [])
        return [{"id": r.get("SubnetId", ""), "name": r.get("SubnetName", ""),
                 "vpc_id": r.get("VpcId", ""), "zone": r.get("Zone", ""),
                 "cidr": r.get("CidrBlock", "")} for r in rows if isinstance(r, dict)]
    raw = ohbs_image._tc3_api("vpc", "DescribeSecurityGroups", "2017-03-12", region,
                              {"Limit": "100"}, sid, key, token)
    rows = raw.get("Response", {}).get("SecurityGroupSet", [])
    return [{"id": r.get("SecurityGroupId", ""), "name": r.get("SecurityGroupName", ""),
             "description": r.get("SecurityGroupDesc", "")} for r in rows if isinstance(r, dict)]


def cmd_discover(args: argparse.Namespace) -> int:
    try:
        rows = discover_resources(args.resource, args.region, zone=args.zone or "",
                                  profile=args.profile or "", vpc_id=args.vpc or "",
                                  min_cpu=getattr(args, "min_cpu", 0) or 0,
                                  min_mem=getattr(args, "min_mem", 0) or 0,
                                  in_stock=bool(getattr(args, "in_stock", False)))
    except Exception as exc:
        fail(f"Discovery failed: {exc}")
        return 1
    if args.output == "json":
        print(json.dumps({"schema": "https://ohbs-image.dev/discover/v1",
                          "resource": args.resource, "region": args.region,
                          "items": rows}, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print("No matching resources found.")
        for row in rows:
            keys = ("id", "name", "zone", "cidr") if args.resource != "instance-types" \
                else ("id", "cpu", "memory", "gpu", "status")
            print("\t".join(str(row.get(k, "")) for k in keys if k in row))
    return 0
