#!/usr/bin/env python3
"""Fail when stable public contracts drift without an explicit snapshot update."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "contracts" / "core-contracts.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_contracts() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from ohbs_image import build_parser
    from ohbs_image._benchmark import BENCHMARK_SCHEMA
    from ohbs_image._extensions import EXTENSION_API_VERSION, ENTRY_POINT_GROUPS, REQUIRED_METHODS
    from ohbs_image._guide import GUIDE_SCHEMA
    from ohbs_image._providers import ENTRY_POINT_GROUP, PROVIDER_API_VERSION, ProviderCapabilities
    from ohbs_image._registry import ARTIFACT_SCHEMA, REGISTRY_SCHEMA
    from ohbs_image._slo import SLO_SCHEMA

    subcommands = sorted(build_parser()._subparsers._group_actions[0].choices)
    contract_files = [ROOT / "api" / "openapi.yaml", *sorted((ROOT / "schemas" / "v1").glob("*.json"))]
    files: dict[str, Any] = {}
    for path in contract_files:
        relative = str(path.relative_to(ROOT))
        schema_id = ""
        if path.suffix == ".json":
            schema_id = str(json.loads(path.read_text(encoding="utf-8")).get("$id") or "")
        files[relative] = {"sha256": _sha(path), "schema_id": schema_id}
    openapi = (ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8")
    operations = sorted(re.findall(r"^\s+operationId:\s*([A-Za-z0-9_]+)\s*$", openapi, re.MULTILINE))
    return {
        "snapshot_version": 1,
        "compatibility_policy": "schemas/COMPATIBILITY.md",
        "cli": {"top_level_commands": subcommands},
        "openapi": {"version": "1.0.0", "operation_ids": operations},
        "provider": {"api_version": PROVIDER_API_VERSION, "entry_point": ENTRY_POINT_GROUP,
                     "capabilities": sorted(ProviderCapabilities.__dataclass_fields__)},
        "extensions": {"api_version": EXTENSION_API_VERSION,
                       "entry_points": dict(sorted(ENTRY_POINT_GROUPS.items())),
                       "required_methods": dict(sorted(REQUIRED_METHODS.items()))},
        "document_schemas": sorted([ARTIFACT_SCHEMA, REGISTRY_SCHEMA, BENCHMARK_SCHEMA, SLO_SCHEMA, GUIDE_SCHEMA]),
        "contract_files": files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Explicitly accept the current contract surface")
    args = parser.parse_args(argv)
    current = current_contracts()
    if args.update:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"updated {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.exists():
        print("core contract snapshot missing; run scripts/check_core_contracts.py --update", file=sys.stderr)
        return 1
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if expected != current:
        print("stable core contract drift detected", file=sys.stderr)
        print("Review compatibility, version changed contracts, then run:", file=sys.stderr)
        print("  python3 scripts/check_core_contracts.py --update", file=sys.stderr)
        return 1
    print(f"core contracts stable: {len(current['cli']['top_level_commands'])} commands, "
          f"{len(current['contract_files'])} contract files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
