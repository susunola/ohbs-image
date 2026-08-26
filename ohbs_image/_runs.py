from __future__ import annotations

import argparse
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail
from ._run_events import read_run_events

RUN_LIST_SCHEMA = "https://ohbs-image.dev/run-list/v1"
RUN_SHOW_SCHEMA = "https://ohbs-image.dev/run-show/v1"


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _entry(index: dict[str, dict[str, Any]], run_id: str) -> dict[str, Any] | None:
    if not run_id or "/" in run_id or "\\" in run_id:
        return None
    return index.setdefault(run_id, {"run_id": run_id, "evidence": []})


def _attach(entry: dict[str, Any], root: Path, path: Path, kind: str) -> None:
    evidence = entry["evidence"]
    if isinstance(evidence, list):
        evidence.append({"kind": kind, "path": str(path.relative_to(root))})


def collect_runs(root: Path | None = None) -> list[dict[str, Any]]:
    """Join every state artifact that can be associated with a run ID."""
    state = root or _lineage_path().parent
    index: dict[str, dict[str, Any]] = {}
    lineage = state / "lineage.jsonl"
    with suppress(OSError):
        for line in lineage.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            item = _entry(index, str(record.get("run_id") or ""))
            if item is not None:
                item["lineage"] = record
                _attach(item, state, lineage, "lineage")

    for path in sorted((state / "runs").glob("*.json")):
        doc = _json_object(path)
        run_id = str(doc.get("run_id") or path.stem) if doc else path.stem
        item = _entry(index, run_id)
        if item is not None:
            if doc:
                item["manifest"] = doc
            _attach(item, state, path, "manifest")

    for path in sorted((state / "plans").glob("*-plan.json")):
        doc = _json_object(path)
        run_id = str(doc.get("run_id") or path.name.removesuffix("-plan.json")) if doc else path.name.removesuffix("-plan.json")
        item = _entry(index, run_id)
        if item is not None:
            if doc:
                item["plan"] = doc
            _attach(item, state, path, "plan")

    releases = state / "releases"
    for path in sorted(releases.glob("*.json")):
        doc = _json_object(path)
        item = _entry(index, str(doc.get("run_id") or "")) if doc else None
        if item is not None:
            item.setdefault("releases", []).append(doc)
            _attach(item, state, path, "release")

    acceptance = state / "acceptance"
    for path in sorted(acceptance.glob("*.json")):
        doc = _json_object(path)
        item = _entry(index, str(doc.get("runId") or doc.get("run_id") or "")) if doc else None
        if item is not None:
            item.setdefault("acceptance", []).append(doc)
            _attach(item, state, path, "acceptance")

    for path in sorted((state / "events").glob("*.jsonl")):
        run_id = path.name.removesuffix(".jsonl")
        item = _entry(index, run_id)
        if item is not None:
            events = read_run_events(run_id, state)
            if events:
                item["events"] = events
            _attach(item, state, path, "events")

    for run_id, item in index.items():
        for directory, kind in (("provenance", "provenance"), ("reports", "report")):
            for path in sorted((state / directory).glob(f"*.{run_id}.*")):
                _attach(item, state, path, kind)
        lineage_doc = item.get("lineage", {})
        manifest_doc = item.get("manifest", {})
        plan_doc = item.get("plan", {})
        item["status"] = (lineage_doc.get("status") or manifest_doc.get("status")
                          or ("planned" if plan_doc else "unknown"))
        events_doc = item.get("events", [])
        item["state"] = (manifest_doc.get("state")
                         or (events_doc[-1].get("to") if events_doc else ""))
        item["mode"] = lineage_doc.get("mode") or manifest_doc.get("mode") or ""
        item["profile"] = (lineage_doc.get("profile") or manifest_doc.get("profile")
                           or plan_doc.get("profile") or "")
        item["created_at"] = (lineage_doc.get("ts") or manifest_doc.get("started_at")
                              or plan_doc.get("generated_at") or "")
        item["evidence_count"] = len(item["evidence"])

    return sorted(index.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)


def cmd_run_list(args: argparse.Namespace) -> int:
    rows = collect_runs()
    if args.profile:
        rows = [row for row in rows if row.get("profile") == args.profile]
    if args.status:
        rows = [row for row in rows if row.get("status") == args.status]
    if args.limit > 0:
        rows = rows[:args.limit]
    summaries = [{key: row.get(key) for key in (
        "run_id", "created_at", "status", "state", "mode", "profile", "evidence_count")}
        for row in rows]
    if args.output == "json":
        print(json.dumps({"schema": RUN_LIST_SCHEMA, "count": len(summaries),
                          "runs": summaries}, ensure_ascii=False, indent=2))
        return 0
    if not summaries:
        print("No runs found in evidence state.")
        return 0
    for row in summaries:
        print(f"{str(row['created_at'] or '?'):20s}  {str(row['status']):8s}  "
              f"{str(row['state'] or '-'):17s}  {str(row['profile'] or '-'):12s}  "
              f"evidence={row['evidence_count']:2d}  {row['run_id']}")
    return 0


def cmd_run_show(args: argparse.Namespace) -> int:
    item = next((row for row in collect_runs() if row["run_id"] == args.run_id), None)
    if item is None:
        fail(f"No state artifacts found for run {args.run_id}")
        return 1
    doc = {"schema": RUN_SHOW_SCHEMA, "run": item}
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    for key in ("run_id", "created_at", "status", "state", "mode", "profile", "evidence_count"):
        print(f"{key}: {item.get(key)}")
    for evidence in item["evidence"]:
        print(f"  {evidence['kind']:10s} {evidence['path']}")
    return 0
