from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ._config import _lineage_path
from ._logging import fail


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
