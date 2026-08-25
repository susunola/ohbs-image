from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail, ok
from ._reports import _read_run_manifest, _render_lineage_html_report

REPORT_LIST_SCHEMA = "https://ohbs-image.dev/report-list/v1"
REPORT_SHOW_SCHEMA = "https://ohbs-image.dev/report-show/v1"
REPORT_COST_SCHEMA = "https://ohbs-image.dev/report-cost/v1"

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


def cmd_report_html(args: argparse.Namespace) -> int:
    """Roadmap F — re-render one run as a self-contained HTML compliance page.

    Reproduces the delivery report for a recorded run from the evidence
    state (lineage + archived audit JSON + provenance), so it works offline
    and long after the build VM is gone.  Useful for re-exporting a report
    to a customer or a GRC mailbox without touching the cloud.
    """
    try:
        rows = _records(_lineage_path())
    except OSError as exc:
        fail(f"Could not read lineage: {exc}")
        return 1
    rec = next((r for r in rows if str(r.get("run_id", "")) == args.run_id), None)
    if rec is None:
        fail(f"No lineage record for run {args.run_id}")
        return 1
    dest = Path(args.output) if getattr(args, "output", None) else None
    out = _render_lineage_html_report(rec, dest=dest)
    if out is None:
        fail(f"Could not render HTML report for run {args.run_id}")
        return 1
    print(f"HTML report written -> {out}")
    return 0


def _fmt_duration(seconds: float) -> str:
    """Format a wall-clock duration as m:ss or h:mm."""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def cmd_report_cost(args: argparse.Namespace) -> int:
    """Aggregate build cost from lineage facts (instance type, spot, time).

    No billing API is called and no stale price table is bundled: the
    command reports the *facts* every record now carries (build VM type,
    spot flag, Packer wall time).  Pass ``--hourly-price USD`` to estimate
    spend (spot runs are billed at 10% of on-demand).  Legacy records that
    predate cost tracking are shown but excluded from totals.
    """
    try:
        rows = _records(_lineage_path())
    except OSError as exc:
        fail(f"Could not read lineage: {exc}")
        return 1
    price = getattr(args, "hourly_price", None)
    priced = isinstance(price, (int, float)) and price > 0
    records: list[dict[str, Any]] = []
    total_seconds = 0.0
    total_cost = 0.0
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        seconds = rec.get("build_seconds")
        seconds_f = float(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else None
        spot = bool(rec.get("spot", False))
        est = None
        if seconds_f is not None:
            total_seconds += seconds_f
            if priced and isinstance(price, (int, float)):
                est = price * (seconds_f / 3600) * (0.1 if spot else 1.0)
                total_cost += est
        records.append({
            "ts": str(rec.get("ts") or "?"),
            "run_id": str(rec.get("run_id") or ""),
            "status": str(rec.get("status") or "?"),
            "mode": str(rec.get("mode") or "build"),
            "profile": str(rec.get("profile") or "?"),
            "region": str(rec.get("region") or "?"),
            "instance_type": str(rec.get("instance_type") or ""),
            "spot": spot,
            "build_seconds": seconds_f,
            "estimated_cost_usd": est,
        })
    totals: dict[str, Any] = {
        "runs": len(records),
        "runs_with_duration": sum(1 for r in records if r["build_seconds"] is not None),
        "wall_hours": round(total_seconds / 3600, 3) if total_seconds else None,
        "estimated_cost_usd": round(total_cost, 4) if priced else None,
    }
    doc: dict[str, Any] = {
        "schema": REPORT_COST_SCHEMA,
        "hourly_price_usd": price if priced else None,
        "records": records,
        "totals": totals,
    }
    if getattr(args, "output", "text") == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    if not records:
        print("No lineage records.")
        return 0
    for rec in records:
        dur = _fmt_duration(rec["build_seconds"]) if rec["build_seconds"] is not None else "-"
        est_s = f"${rec['estimated_cost_usd']:.4f}" if rec["estimated_cost_usd"] is not None else "-"
        spot_s = " spot" if rec["spot"] else ""
        print(f"{rec['ts']}  {rec['status']:6s}  {rec['profile']:12s}  "
              f"{rec['region']:16s}  {rec['instance_type'] or '-':14s}  "
              f"{dur:>7s}  est {est_s:>9s}{spot_s}")
    totals = doc["totals"]
    print("-" * 78)
    print(f"runs tracked: {totals['runs']}  |  with duration: "
          f"{totals['runs_with_duration']}  |  wall hours: "
          f"{totals['wall_hours'] if totals['wall_hours'] is not None else '-'}")
    if priced:
        print(f"estimated spend (on-demand ${price:g}/h, spot at 10%): "
              f"${totals['estimated_cost_usd']:.4f}")
    else:
        print("pass --hourly-price USD to estimate spend from the recorded "
              "durations (spot runs at 10% of on-demand)")
    return 0
