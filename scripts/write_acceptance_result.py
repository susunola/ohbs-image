#!/usr/bin/env python3
"""Write a portable real-cloud acceptance result and GitHub summary."""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_result(args: argparse.Namespace) -> dict[str, object]:
    status = "passed" if args.status == "success" else "failed"
    run_url = args.run_url or (
        f"{_env('GITHUB_SERVER_URL', 'https://github.com')}/{_env('GITHUB_REPOSITORY')}"
        f"/actions/runs/{_env('GITHUB_RUN_ID')}"
    )
    result: dict[str, object] = {
        "schemaVersion": 1,
        "status": status,
        "profile": args.profile,
        "level": int(args.level),
        "commit": args.commit or _env("GITHUB_SHA"),
        "workflow": args.workflow or _env("GITHUB_WORKFLOW"),
        "runId": _env("GITHUB_RUN_ID"),
        "runAttempt": int(_env("GITHUB_RUN_ATTEMPT", "1")),
        "runUrl": run_url,
        "startedAt": args.started_at or _env("OHBS_ACCEPTANCE_STARTED_AT"),
        "finishedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "evidenceArtifact": args.artifact,
    }
    if args.build_instance_type:
        result["buildInstanceType"] = args.build_instance_type
    return result


def write_summary(result: dict[str, object], path: Path) -> None:
    icon = "✅" if result["status"] == "passed" else "❌"
    rows = [
        "## Real-cloud acceptance",
        "",
        f"{icon} **{str(result['status']).upper()}**",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Profile | `{result['profile']}` |",
        f"| CIS level | `{result['level']}` |",
        f"| Commit | `{result['commit']}` |",
        f"| Started | `{result['startedAt']}` |",
        f"| Finished | `{result['finishedAt']}` |",
        f"| Evidence artifact | `{result['evidenceArtifact']}` |",
        f"| Workflow run | [Open run]({result['runUrl']}) |",
        "",
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", required=True, choices=("success", "failure", "cancelled"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--level", required=True, choices=("1", "2"))
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output", type=Path, default=Path("acceptance-result.json"))
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--commit", default="")
    parser.add_argument("--workflow", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--build-instance-type", default="")
    args = parser.parse_args(argv)

    result = build_result(args)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(result, args.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
