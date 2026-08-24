from __future__ import annotations

import argparse
import json
import os
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
                       profile: str = "") -> list[dict[str, Any]]:
    sid, key, token = _credentials()
    if kind == "images":
        params: dict[str, Any] = {"ImageType": "PUBLIC_IMAGE", "Limit": 100}
        raw = ohbs_image._tc3_api("cvm", "DescribeImages", "2017-03-12", region,
                                  params, sid, key, token)
        rows = raw.get("Response", {}).get("ImageSet", [])
        result = []
        profile_needles = {
            "ubuntu2004": ("ubuntu", "20.04"), "ubuntu2204": ("ubuntu", "22.04"),
            "ubuntu2404": ("ubuntu", "24.04"), "rhel8": ("rhel", "8"),
            "rhel9": ("rhel", "9"), "rhel10": ("rhel", "10"),
            "tencentos3": ("tencent", "3"), "tencentos4": ("tencent", "4"),
            "win2016": ("windows", "2016"), "win2019": ("windows", "2019"),
            "win2022": ("windows", "2022"), "win2025": ("windows", "2025"),
        }
        needles = profile_needles.get(profile, ())
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("ImageName", ""))
            haystack = f"{name} {row.get('OsName', '')}".lower()
            if needles and not all(needle in haystack for needle in needles):
                continue
            result.append({"id": row.get("ImageId", ""), "name": name,
                           "os": row.get("OsName", ""), "created_at": row.get("CreatedTime", "")})
        return result
    if kind == "vpcs":
        raw = ohbs_image._tc3_api("vpc", "DescribeVpcs", "2017-03-12", region,
                                  {"Limit": "100"}, sid, key, token)
        rows = raw.get("Response", {}).get("VpcSet", [])
        return [{"id": r.get("VpcId", ""), "name": r.get("VpcName", ""),
                 "cidr": r.get("CidrBlock", "")} for r in rows if isinstance(r, dict)]
    if kind == "subnets":
        filters = [{"Name": "zone", "Values": [zone]}] if zone else []
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
                                  profile=args.profile or "")
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
            print("\t".join(str(row.get(k, "")) for k in ("id", "name", "zone", "cidr")
                            if k in row))
    return 0
