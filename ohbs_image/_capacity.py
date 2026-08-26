from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._config import ResolvedConfig
from ._discover import discover_resources
from ._registry import _hash

CAPACITY_PLAN_SCHEMA = "https://ohbs-image.dev/capacity-fallback/v1"
Discover = Callable[..., list[dict[str, Any]]]
_FIELDS = ("region", "zone", "instance_type", "vpc_id", "subnet_id", "security_group_id")


def load_capacity_plan(path: Path) -> list[dict[str, str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid capacity plan {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != CAPACITY_PLAN_SCHEMA:
        raise ValueError("capacity plan schema mismatch")
    rows = value.get("candidates")
    if not isinstance(rows, list) or not rows:
        raise ValueError("capacity plan requires at least one candidate")
    candidates: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"capacity candidate {index} must be an object")
        candidate = {field: str(row.get(field) or "") for field in _FIELDS}
        if any(not candidate[field] for field in _FIELDS):
            raise ValueError(f"capacity candidate {index} requires all placement fields")
        candidates.append(candidate)
    return candidates


def select_capacity(candidates: list[dict[str, str]], *,
                    discover: Discover = discover_resources) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for priority, candidate in enumerate(candidates):
        try:
            rows = discover("instance-types", candidate["region"],
                            zone=candidate["zone"], in_stock=True)
        except OSError as exc:
            attempts.append({"priority": priority, **candidate,
                             "available": False, "reason": str(exc)})
            continue
        available = any(str(row.get("id") or "") == candidate["instance_type"]
                        for row in rows)
        attempts.append({"priority": priority, **candidate, "available": available,
                         "reason": "in-stock" if available else "not-purchasable"})
        if available:
            decision: dict[str, Any] = {"schema": CAPACITY_PLAN_SCHEMA,
                "selected": candidate, "selected_priority": priority,
                "fallback_used": priority > 0, "attempts": attempts}
            decision["document_hash"] = _hash(decision)
            return decision
    raise ValueError("no capacity candidate is currently purchasable")


def apply_capacity(r: ResolvedConfig, decision: dict[str, Any]) -> None:
    selected = decision.get("selected")
    if not isinstance(selected, dict):
        raise ValueError("capacity decision has no selected placement")
    for field in _FIELDS:
        setattr(r, field, str(selected[field]))
