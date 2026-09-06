from __future__ import annotations

import argparse
import json

from ohbs_image._discover import cmd_discover, discover_resources, rank_instance_types


def test_discover_subnets_normalizes_cloud_response(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")

    def fake_api(service, action, version, region, params, sid, key, token):
        assert (service, action, region) == ("vpc", "DescribeSubnets", "ap-guangzhou")
        assert params["Filters"] == [
            {"Name": "zone", "Values": ["ap-guangzhou-3"]},
            {"Name": "vpc-id", "Values": ["vpc-1"]},
        ]
        return {"Response": {"SubnetSet": [{"SubnetId": "subnet-1", "SubnetName": "build",
                                              "VpcId": "vpc-1", "Zone": "ap-guangzhou-3",
                                              "CidrBlock": "10.0.1.0/24"}]}}

    monkeypatch.setattr("ohbs_image._tc3_api", fake_api)
    rows = discover_resources("subnets", "ap-guangzhou", zone="ap-guangzhou-3", vpc_id="vpc-1")
    assert rows == [{"id": "subnet-1", "name": "build", "vpc_id": "vpc-1",
                     "zone": "ap-guangzhou-3", "cidr": "10.0.1.0/24"}]


def test_discover_images_filters_profile(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    def fake_api(*args, **kwargs):
        assert args[4] == {"Limit": 100}
        return {
        "Response": {"ImageSet": [
            {"ImageId": "img-u", "ImageName": "Ubuntu Server 24.04", "OsName": "Ubuntu",
             "ImageType": "PUBLIC_IMAGE"},
            {"ImageId": "img-private", "ImageName": "Ubuntu Server 24.04 private",
             "OsName": "Ubuntu", "ImageType": "PRIVATE_IMAGE"},
            {"ImageId": "img-r", "ImageName": "RHEL 9", "OsName": "RHEL"},
        ]}}
    monkeypatch.setattr("ohbs_image._tc3_api", fake_api)
    rows = discover_resources("images", "ap-guangzhou", profile="ubuntu2404")
    assert [row["id"] for row in rows] == ["img-u"]
    assert set(rows[0]) == {"id", "name", "os", "architecture", "state", "created_at"}


def test_discover_images_matches_tencentos_major_without_digit_collisions(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    monkeypatch.setattr("ohbs_image._tc3_api", lambda *args, **kwargs: {
        "Response": {"ImageSet": [
            {"ImageId": "img-tos3", "ImageName": "TencentOS Server 3.3 (TK4)",
             "OsName": "TencentOS Server 3.3 (TK4)", "ImageType": "PUBLIC_IMAGE"},
            {"ImageId": "img-tos4", "ImageName": "TencentOS Server 4 for x86_64",
             "OsName": "TencentOS Server 4 for x86_64", "ImageType": "PUBLIC_IMAGE"},
        ]}}
    )

    rows = discover_resources("images", "ap-guangzhou", profile="tencentos4")

    assert [row["id"] for row in rows] == ["img-tos4"]


def test_discover_v1_json_contract(monkeypatch, capsys):
    monkeypatch.setattr("ohbs_image._discover.discover_resources",
                        lambda *a, **k: [{"id": "vpc-1", "name": "build"}])
    args = argparse.Namespace(resource="vpcs", region="ap-guangzhou", zone=None,
                              profile=None, vpc=None, output="json")
    assert cmd_discover(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "https://ohbs-image.dev/discover/v1", "resource": "vpcs",
        "region": "ap-guangzhou", "items": [{"id": "vpc-1", "name": "build"}]}


def test_discover_instance_types_requires_zone(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    import pytest
    with pytest.raises(OSError, match="requires --zone"):
        discover_resources("instance-types", "ap-guangzhou")


def test_discover_instance_types_filters_and_normalizes(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")

    def fake_api(service, action, version, region, params, sid, key, token):
        assert (service, action, version, region) == (
            "cvm", "DescribeZoneInstanceConfigInfos", "2017-03-12", "ap-guangzhou")
        assert "InstanceChargeType" not in params
        assert params["Filters"] == [{"Name": "zone", "Values": ["ap-guangzhou-3"]}]
        return {"Response": {"InstanceTypeQuotaSet": [
            {"InstanceType": "S5.MEDIUM2", "Cpu": 2, "Memory": 4, "GPU": 0,
             "Status": "AVAILABLE", "InstanceChargeType": "PREPAID"},
            {"InstanceType": "S5.MEDIUM2", "Cpu": 2, "Memory": 4, "GPU": 0,
             "Status": "AVAILABLE", "InstanceChargeType": "POSTPAID_BY_HOUR"},
            {"InstanceType": "S5.LARGE8", "Cpu": 4, "Memory": 8, "GPU": 0,
             "Status": "AVAILABLE", "InstanceChargeType": "POSTPAID_BY_HOUR"},
            {"InstanceType": "S5.LARGE4", "Cpu": 4, "Memory": 4, "GPU": 0,
             "Status": "SOLD_OUT", "InstanceChargeType": "POSTPAID_BY_HOUR"},
        ]}}

    monkeypatch.setattr("ohbs_image._tc3_api", fake_api)
    rows = discover_resources("instance-types", "ap-guangzhou", zone="ap-guangzhou-3",
                              min_cpu=4, min_mem=8)
    assert rows == [{"id": "S5.LARGE8", "name": "S5.LARGE8", "zone": "ap-guangzhou-3",
                     "cpu": 4, "memory": 8, "gpu": 0, "status": "AVAILABLE"}]
    in_stock = discover_resources("instance-types", "ap-guangzhou", zone="ap-guangzhou-3",
                                  in_stock=True)
    assert [r["id"] for r in in_stock] == ["S5.MEDIUM2", "S5.LARGE8"]


def test_instance_ranking_prefers_observed_reliability_then_p95() -> None:
    candidates = [
        {"id": "S5.SMALL2", "cpu": 1, "memory": 2, "status": "AVAILABLE"},
        {"id": "S5.MEDIUM2", "cpu": 2, "memory": 4, "status": "AVAILABLE"},
        {"id": "S5.LARGE8", "cpu": 4, "memory": 8, "status": "AVAILABLE"},
    ]
    history = [
        {"instance_type": "S5.SMALL2", "status": "failed", "build_seconds": 300},
        {"instance_type": "S5.MEDIUM2", "status": "completed", "build_seconds": 200},
        {"instance_type": "S5.MEDIUM2", "status": "completed", "build_seconds": 180},
        {"instance_type": "S5.LARGE8", "status": "completed", "build_seconds": 240},
    ]
    result = rank_instance_types(candidates, history)
    assert [row["id"] for row in result] == ["S5.MEDIUM2", "S5.LARGE8", "S5.SMALL2"]
    assert result[0]["recommended"] is True
    assert result[0]["selection_evidence"] == {"samples": 2, "failure_rate": 0.0,
        "p95_build_seconds": 200.0, "reason": "2 builds; failure_rate=0.0%; p95=200.0s"}


def test_instance_ranking_without_history_is_deterministic_and_stock_safe() -> None:
    result = rank_instance_types([
        {"id": "B", "cpu": 2, "memory": 4, "status": "SOLD_OUT"},
        {"id": "A", "cpu": 2, "memory": 4, "status": "AVAILABLE"},
    ])
    assert [row["id"] for row in result] == ["A", "B"]
