from __future__ import annotations

import argparse
import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from ._channels import collect_channels
from ._logging import fail, ok, warn
from ._registry import (
    _artifact_path,
    _hash,
    _read_object,
    change_artifact_status,
    collect_artifacts,
)
from ._reports import _atomic_write_bytes, _state_lock

ANCESTRY_SCHEMA = "https://ohbs-image.dev/ancestry-impact/v1"


def _parent_ids(artifact: dict[str, Any]) -> list[str]:
    parents = artifact.get("parents")
    if not isinstance(parents, list):
        return []
    return [str(item.get("artifact_id")) for item in parents
            if isinstance(item, dict) and item.get("artifact_id")]


def _artifact_map(root: Path | None = None) -> dict[str, dict[str, Any]]:
    return {str(item.get("artifact_id")): item for item in collect_artifacts(root)
            if item.get("artifact_id")}


def descendants(artifact_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    artifacts = _artifact_map(root)
    children: dict[str, list[str]] = {}
    for child_id, artifact in artifacts.items():
        for parent_id in _parent_ids(artifact):
            children.setdefault(parent_id, []).append(child_id)
    rows: list[dict[str, Any]] = []
    queue: deque[tuple[str, int]] = deque((child, 1)
                                          for child in sorted(children.get(artifact_id, [])))
    seen: set[str] = set()
    while queue:
        child_id, depth = queue.popleft()
        if child_id in seen:
            continue
        seen.add(child_id)
        artifact = artifacts[child_id]
        rows.append({"artifact_id": child_id, "depth": depth,
                     "status": artifact.get("status"), "bucket": artifact.get("bucket")})
        queue.extend((nested, depth + 1)
                     for nested in sorted(children.get(child_id, [])))
    return rows


def verify_ancestry(root: Path | None = None) -> list[str]:
    artifacts = _artifact_map(root)
    failures: list[str] = []
    for artifact_id, artifact in artifacts.items():
        parents = _parent_ids(artifact)
        if artifact_id in parents:
            failures.append(f"{artifact_id}: artifact cannot be its own parent")
        if len(parents) != len(set(parents)):
            failures.append(f"{artifact_id}: duplicate parent relationship")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(artifact_id: str, trail: list[str]) -> None:
        if artifact_id in visiting:
            start = trail.index(artifact_id)
            failures.append("ancestry cycle: " + " -> ".join([*trail[start:], artifact_id]))
            return
        if artifact_id in visited or artifact_id not in artifacts:
            return
        visiting.add(artifact_id)
        for parent_id in _parent_ids(artifacts[artifact_id]):
            visit(parent_id, [*trail, artifact_id])
        visiting.remove(artifact_id)
        visited.add(artifact_id)

    for artifact_id in sorted(artifacts):
        visit(artifact_id, [])
    return sorted(set(failures))


def link_parent(child_id: str, parent_id: str, *, relation: str = "derived_from",
                allow_external: bool = False, root: Path | None = None) -> dict[str, Any]:
    child_path = _artifact_path(child_id, root)
    if child_id == parent_id:
        raise ValueError("artifact cannot be its own parent")
    parent = _read_object(_artifact_path(parent_id, root))
    if parent is None and not allow_external:
        raise ValueError(f"parent artifact {parent_id} not found; use --external for vendor images")
    lock = _state_lock(child_path)
    try:
        child = _read_object(child_path)
        if child is None or child.get("document_hash") != _hash(child):
            raise ValueError(f"child artifact {child_id} not found or failed integrity verification")
        parents = child.get("parents")
        rows = list(parents) if isinstance(parents, list) else []
        if any(isinstance(item, dict) and item.get("artifact_id") == parent_id for item in rows):
            return child
        rows.append({"artifact_id": parent_id, "relation": relation,
                     "external": parent is None})
        child["parents"] = rows
        child["document_hash"] = _hash(child)
        _atomic_write_bytes(child_path,
                            (json.dumps(child, ensure_ascii=False, indent=2) + "\n").encode())
    finally:
        lock.rmdir()
    failures = verify_ancestry(root)
    if failures:
        # Restore by removing only the relationship just added.
        lock = _state_lock(child_path)
        try:
            child = _read_object(child_path) or child
            child["parents"] = [item for item in child.get("parents", [])
                                if not (isinstance(item, dict)
                                        and item.get("artifact_id") == parent_id)]
            child["document_hash"] = _hash(child)
            _atomic_write_bytes(child_path,
                                (json.dumps(child, ensure_ascii=False, indent=2) + "\n").encode())
        finally:
            lock.rmdir()
        raise ValueError("; ".join(failures))
    return child


def impact_plan(artifact_id: str, root: Path | None = None) -> dict[str, Any]:
    artifacts = _artifact_map(root)
    if artifact_id not in artifacts:
        raise ValueError(f"artifact {artifact_id} not found")
    affected = [{"artifact_id": artifact_id, "depth": 0,
                 "status": artifacts[artifact_id].get("status"),
                 "bucket": artifacts[artifact_id].get("bucket")},
                *descendants(artifact_id, root)]
    ids = {row["artifact_id"] for row in affected}
    channels = [{"bucket": item.get("bucket"), "channel": item.get("channel"),
                 "artifact_id": item.get("artifact_id"), "generation": item.get("generation")}
                for item in collect_channels(root) if item.get("artifact_id") in ids]
    plan: dict[str, Any] = {
        "schema": ANCESTRY_SCHEMA, "root_artifact_id": artifact_id,
        "affected_count": len(affected), "descendant_count": len(affected) - 1,
        "channel_count": len(channels), "artifacts": affected, "channels": channels,
    }
    plan["document_hash"] = _hash(plan)
    return plan


def cascade_revoke(artifact_id: str, *, actor: str, reason: str,
                   apply: bool = False, root: Path | None = None) -> dict[str, Any]:
    plan = impact_plan(artifact_id, root)
    plan["apply"] = apply
    plan["results"] = []
    if not apply:
        plan["document_hash"] = _hash(plan)
        return plan
    # Deepest descendants first, then the root; each status change handles its channels.
    ordered = sorted(plan["artifacts"], key=lambda item: int(item["depth"]), reverse=True)
    for item in ordered:
        target = str(item["artifact_id"])
        if item.get("status") == "revoked":
            plan["results"].append({"artifact_id": target, "status": "already_revoked"})
            continue
        result = change_artifact_status(target, "revoked", actor=actor, reason=reason,
                                        auto_rollback=True, root=root)
        plan["results"].append({"artifact_id": target, "status": "revoked",
                                "channel_rollbacks": result["channel_rollbacks"]})
    plan["document_hash"] = _hash(plan)
    return plan


def cmd_ancestry_descendants(args: argparse.Namespace) -> int:
    rows = descendants(args.artifact_id)
    if args.output == "json":
        print(json.dumps({"artifact_id": args.artifact_id, "descendants": rows}, indent=2))
    else:
        for item in rows:
            print(f"{'  ' * int(item['depth'])}{item['artifact_id']} ({item['status']})")
    return 0


def cmd_ancestry_link(args: argparse.Namespace) -> int:
    try:
        link_parent(args.child, args.parent, relation=args.relation,
                    allow_external=args.external)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    ok(f"Linked {args.parent} -> {args.child}")
    return 0


def cmd_ancestry_impact(args: argparse.Namespace) -> int:
    try:
        plan = impact_plan(args.artifact_id)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    if args.output == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"{plan['affected_count']} artifact(s), {plan['channel_count']} channel(s) affected")
        for item in plan["artifacts"]:
            print(f"depth={item['depth']} {item['artifact_id']} ({item['status']})")
    return 0


def cmd_ancestry_revoke(args: argparse.Namespace) -> int:
    actor = args.actor or os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown"
    try:
        result = cascade_revoke(args.artifact_id, actor=actor, reason=args.reason,
                                apply=args.apply)
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.apply:
        ok(f"Revoked {len(result['results'])} artifact(s) in descendant-first order")
    else:
        warn(f"Dry run: would revoke {result['affected_count']} artifact(s); add --apply")
    return 0


def cmd_ancestry_verify(args: argparse.Namespace) -> int:
    failures = verify_ancestry()
    if args.output == "json":
        print(json.dumps({"valid": not failures, "failures": failures}, indent=2))
    elif failures:
        for message in failures:
            fail(message)
    else:
        ok("Ancestry graph is valid")
    return 1 if failures else 0
