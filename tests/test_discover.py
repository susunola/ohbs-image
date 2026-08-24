from __future__ import annotations

from ohbs_image._discover import discover_resources


def test_discover_subnets_normalizes_cloud_response(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")

    def fake_api(service, action, version, region, params, sid, key, token):
        assert (service, action, region) == ("vpc", "DescribeSubnets", "ap-guangzhou")
        assert params["Filters"][0]["Values"] == ["ap-guangzhou-3"]
        return {"Response": {"SubnetSet": [{"SubnetId": "subnet-1", "SubnetName": "build",
                                              "VpcId": "vpc-1", "Zone": "ap-guangzhou-3",
                                              "CidrBlock": "10.0.1.0/24"}]}}

    monkeypatch.setattr("ohbs_image._tc3_api", fake_api)
    rows = discover_resources("subnets", "ap-guangzhou", zone="ap-guangzhou-3")
    assert rows == [{"id": "subnet-1", "name": "build", "vpc_id": "vpc-1",
                     "zone": "ap-guangzhou-3", "cidr": "10.0.1.0/24"}]


def test_discover_images_filters_profile(monkeypatch):
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "sid")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "key")
    monkeypatch.setattr("ohbs_image._tc3_api", lambda *a, **k: {
        "Response": {"ImageSet": [
            {"ImageId": "img-u", "ImageName": "Ubuntu Server 24.04", "OsName": "Ubuntu"},
            {"ImageId": "img-r", "ImageName": "RHEL 9", "OsName": "RHEL"},
        ]}})
    rows = discover_resources("images", "ap-guangzhou", profile="ubuntu2404")
    assert [row["id"] for row in rows] == ["img-u"]
