from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._providers import PROVIDER_API_VERSION, ProviderCapabilities, ProviderCredentials


class AwsContractProvider:
    """Offline AWS contract PoC; it never performs network or billable work."""

    name = "aws-contract-poc"
    api_version = PROVIDER_API_VERSION
    maturity = "contract-poc"
    capabilities = ProviderCapabilities(
        compute=True,
        images=True,
        networking=True,
        distribution=True,
        discovery=True,
    )
    _actions = {
        "RunInstances",
        "DescribeInstances",
        "CreateImage",
        "DescribeImages",
        "CopyImage",
        "CreateTags",
        "TerminateInstances",
    }

    def request(
        self,
        service: str,
        action: str,
        version: str,
        region: str,
        params: dict[str, Any],
        credentials: ProviderCredentials,
        *,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Return a deterministic unsigned request plan, never an API response."""
        del credentials
        if service != "ec2":
            raise ValueError("AWS contract PoC only models the EC2 service")
        if action not in self._actions:
            raise ValueError(f"unsupported AWS contract action: {action}")
        if not region or not version:
            raise ValueError("region and API version are required")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        return {
            "ContractOnly": True,
            "NetworkSent": False,
            "BillableOperation": False,
            "RequestPlan": {
                "service": service,
                "action": action,
                "version": version,
                "region": region,
                "params": deepcopy(params),
                "max_retries": max_retries,
            },
        }

    def discover(self, resource: str, region: str, **filters: Any) -> list[dict[str, Any]]:
        """Filter caller-supplied fixtures; no AWS inventory is queried."""
        if resource not in {"images", "instances", "subnets"}:
            raise ValueError(f"unsupported AWS contract resource: {resource}")
        if not region:
            raise ValueError("region is required")
        fixtures = filters.pop("fixtures", [])
        if not isinstance(fixtures, list) or not all(isinstance(item, dict) for item in fixtures):
            raise TypeError("fixtures must be a list of objects")
        result: list[dict[str, Any]] = []
        for item in fixtures:
            if item.get("region", region) != region:
                continue
            if all(item.get(key) == value for key, value in filters.items()):
                result.append(deepcopy(item))
        return result

    def contract_test(self) -> dict[str, Any]:
        credentials = ProviderCredentials("not-used", "not-used")
        lifecycle = (
            ("launch", "RunInstances", {"ImageId": "ami-fixture", "MinCount": 1, "MaxCount": 1}),
            ("wait", "DescribeInstances", {"InstanceIds": ["i-fixture"]}),
            ("snapshot", "CreateImage", {"InstanceId": "i-fixture", "Name": "golden-fixture"}),
            ("publish", "CopyImage", {"SourceImageId": "ami-fixture", "SourceRegion": "us-east-1"}),
            ("cleanup", "TerminateInstances", {"InstanceIds": ["i-fixture"]}),
        )
        checks: list[dict[str, Any]] = []
        for phase, action, params in lifecycle:
            plan = self.request("ec2", action, "2016-11-15", "us-east-1", params, credentials)
            checks.append({
                "name": phase,
                "action": action,
                "passed": plan["ContractOnly"] and not plan["NetworkSent"] and not plan["BillableOperation"],
            })
        fixture = {"id": "ami-fixture", "region": "us-east-1", "state": "available"}
        discovered = self.discover("images", "us-east-1", fixtures=[fixture], state="available")
        checks.append({"name": "discovery", "passed": discovered == [fixture]})
        return {
            "schema": "ohbs-image/provider-contract/v1",
            "offline": True,
            "production_ready": False,
            "checks": checks,
            "limitations": [
                "does not sign or send AWS requests",
                "does not prove IAM, quota, eventual-consistency, or image boot behaviour",
                "must not be used as a production AWS provider",
            ],
        }
