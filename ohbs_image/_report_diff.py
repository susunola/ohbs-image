from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail, ok
from ._reports import _read_run_manifest

REPORT_LIST_SCHEMA = "https://ohbs-image.dev/report-list/v1"
REPORT_SHOW_SCHEMA = "https://ohbs-image.dev/report-show/v1"

# Fields shown in `report list` text output, in order.
_LIST_COLUMNS = ("ts", "status", "mode", "profile", "cis_level", "score",
                 "image_name", "run_id")


def _records(path: Path) -> list[dict[str, Any]]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def cmd_report_list(args: argparse.Namespace) -> int:
    """Roadmap F — enumerate lineage records (newest first) with filters.

    Complements `report diff`, which needs run IDs. JSON follows the
    `report-list/v1` contract so CI can consume the evidence index.
    """
    try:
        rows = _records(_lineage_path())
    except OSError as exc:
        fail(f"Could not read lineage: {exc}")
        return 1
    rows.reverse()  # newest first
    if args.profile:
        rows = [r for r in rows if r.get("profile") == args.profile]
    if args.status:
        rows = [r for r in rows if r.get("status") == args.status]
    if args.mode:
        rows = [r for r in rows if r.get("mode") == args.mode]
    limit = getattr(args, "limit", 20)
    if limit > 0:
        rows = rows[:limit]
    if args.output == "json":
        print(json.dumps({"schema": REPORT_LIST_SCHEMA, "count": len(rows),
                          "records": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No lineage records match.")
        return 0
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        score = rec.get("score")
        score_s = f"{score:g}%" if isinstance(score, (int, float)) else "-"
        level = rec.get("cis_level")
        print(f"{rec.get('ts', '?'):s}  {str(rec.get('status') or '?'):6s}  "
              f"{str(rec.get('mode') or 'build'):5s}  L{level if level is not None else '?'}  "
              f"score={score_s:>6s}  {str(rec.get('profile') or '?'):s}  "
              f"{str(rec.get('image_name') or ''):s}  {str(rec.get('run_id') or ''):s}")
    return 0


def cmd_report_show(args: argparse.Namespace) -> int:
    """Roadmap F — single-run evidence summary (lineage + run manifest)."""
    try:
        rows = _records(_lineage_path())
    except OSError as exc:
        fail(f"Could not read lineage: {exc}")
        return 1
    rec = next((r for r in rows if str(r.get("run_id", "")) == args.run_id), None)
    if rec is None:
        fail(f"No lineage record for run {args.run_id}")
        return 1
    doc: dict[str, Any] = {"schema": REPORT_SHOW_SCHEMA, "record": rec}
    manifest = _read_run_manifest(args.run_id)
    if manifest:
        doc["manifest"] = manifest
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        for key in ("ts", "status", "mode", "profile", "cis_level", "region",
                    "zone", "source_image_id", "image_name", "image_ids",
                    "score", "scope", "benchmark", "fingerprint", "run_id"):
            if key in rec:
                print(f"{key}: {rec[key]}")
        if "sbom_sha256" in rec:
            print(f"sbom_sha256: {rec['sbom_sha256']}")
        if "sbom_packages" in rec:
            print(f"sbom_packages: {rec['sbom_packages']}")
        if manifest:
            ok(f"run manifest: {manifest.get('status', '?')} "
               f"(phase {manifest.get('phase', '?')})")
    return 0


def cmd_report_diff(args: argparse.Namespace) -> int:
    path = _lineage_path()
    try:
        rows = _records(path)
    except OSError as exc:
        fail(f"Could not read lineage: {exc}")
        return 1
    by_id = {str(row.get("run_id", "")): row for row in rows}
    before = by_id.get(args.before)
    after = by_id.get(args.after)
    if not before or not after:
        fail("Both --before and --after run IDs must exist in lineage")
        return 1
    keys = ("status", "profile", "cis_level", "region", "zone", "source_image_id",
            "score", "benchmark", "fingerprint", "sbom_sha256", "sbom_packages")
    changes = [{"field": key, "before": before.get(key), "after": after.get(key)}
               for key in keys if before.get(key) != after.get(key)]
    doc = {"schema": "https://ohbs-image.dev/report-diff/v1",
           "before": args.before, "after": args.after, "changes": changes}
    if args.output == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    elif not changes:
        print("No tracked build metadata changed.")
    else:
        for change in changes:
            print(f"{change['field']}: {change['before']} -> {change['after']}")
    return 0
