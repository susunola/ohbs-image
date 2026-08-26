#!/usr/bin/env python3
"""Build a bounded real-cloud acceptance matrix without creating resources."""
from __future__ import annotations

import argparse
import json
from datetime import date

PROFILES = (
    "ubuntu2004", "ubuntu2204", "ubuntu2404", "rhel8", "rhel9", "rhel10",
    "rocky9", "tencentos3", "tencentos4", "win2016", "win2019", "win2022", "win2025",
)


def source_secret(profile: str) -> str:
    return f"TC_MATRIX_{profile.upper()}_IMAGE_ID"


def build_matrix(tier: str, *, week: int | None = None, max_jobs: int = 2) -> list[dict[str, str]]:
    if max_jobs < 1 or max_jobs > 26:
        raise ValueError("max_jobs must be between 1 and 26")
    if tier == "rotation":
        profile = PROFILES[(week if week is not None else date.today().isocalendar().week) % len(PROFILES)]
        selected = [(profile, "1"), (profile, "2")]
    elif tier == "representative":
        selected = [("tencentos3", "1"), ("ubuntu2404", "2"), ("win2022", "1")]
    elif tier == "full":
        selected = [(profile, level) for profile in PROFILES for level in ("1", "2")]
    else:
        raise ValueError(f"unknown acceptance tier {tier}")
    if len(selected) > max_jobs:
        selected = selected[:max_jobs]
    return [{"profile": profile, "level": level, "source_secret": source_secret(profile)}
            for profile, level in selected]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("rotation", "representative", "full"),
                        default="rotation")
    parser.add_argument("--week", type=int)
    parser.add_argument("--max-jobs", type=int, default=2)
    parser.add_argument("--github-output", default="")
    args = parser.parse_args(argv)
    matrix = {"include": build_matrix(args.tier, week=args.week, max_jobs=args.max_jobs)}
    payload = json.dumps(matrix, separators=(",", ":"))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={payload}\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
