from __future__ import annotations

import json

import pytest

from ohbs_image._capacity import CAPACITY_PLAN_SCHEMA, load_capacity_plan, select_capacity


def _candidate(zone: str, instance_type: str) -> dict[str, str]:
    return {"region": "ap-guangzhou", "zone": zone,
            "instance_type": instance_type, "vpc_id": f"vpc-{zone}",
            "subnet_id": f"subnet-{zone}", "security_group_id": f"sg-{zone}"}


def test_capacity_selects_first_in_stock_fallback_and_records_attempts():
    candidates = [_candidate("zone-a", "S5.MEDIUM2"),
                  _candidate("zone-b", "SA5.MEDIUM2")]

    def discover(_kind, _region, *, zone, in_stock):
        assert in_stock is True
        return [] if zone == "zone-a" else [{"id": "SA5.MEDIUM2"}]

    decision = select_capacity(candidates, discover=discover)
    assert decision["selected_priority"] == 1
    assert decision["fallback_used"] is True
    assert [row["available"] for row in decision["attempts"]] == [False, True]
    assert decision["document_hash"]


def test_capacity_fails_closed_when_every_candidate_is_unavailable():
    with pytest.raises(ValueError, match="no capacity candidate"):
        select_capacity([_candidate("zone-a", "S5.MEDIUM2")],
                        discover=lambda *_args, **_kwargs: [])


def test_capacity_plan_requires_complete_cross_region_network_mapping(tmp_path):
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps({"schema": CAPACITY_PLAN_SCHEMA,
        "candidates": [{"region": "ap-guangzhou", "zone": "zone-a",
                        "instance_type": "S5.MEDIUM2"}]}))
    with pytest.raises(ValueError, match="all placement fields"):
        load_capacity_plan(path)
