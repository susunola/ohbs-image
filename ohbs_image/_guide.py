"""Role-based CLI discovery for the primary ohbs-image product journeys."""
from __future__ import annotations

import argparse
import json
from typing import Any

GUIDE_SCHEMA = "https://ohbs-image.dev/guide/v1"

JOURNEYS: dict[str, dict[str, Any]] = {
    "builder": {
        "title": "Golden-image builder",
        "outcome": "Build and verify a hardened image with release evidence.",
        "steps": [
            ("Experience the deliverable offline", "ohbs-image try"),
            ("Create a minimal configuration", "ohbs-image configure"),
            ("Check tools, credentials and cloud access", "ohbs-image doctor"),
            ("Preview cost, risk and outputs", "ohbs-image plan --check"),
            ("Run the resumable build journey", "ohbs-image launch --build --yes"),
        ],
    },
    "security": {
        "title": "Security and compliance",
        "outcome": "Assess controls, explain policy decisions and review evidence.",
        "steps": [
            ("Audit without remediation", "ohbs-image scan --html report.html"),
            ("Inspect a run and its evidence", "ohbs-image run show RUN_ID"),
            ("Evaluate a release policy", "ohbs-image policy check --help"),
            ("Explain the effective policy", "ohbs-image policy explain --help"),
            ("Generate a technical compliance pack", "ohbs-image compliance assess --help"),
        ],
    },
    "platform": {
        "title": "Platform operations",
        "outcome": "Operate image state, promotion, distribution and recovery.",
        "steps": [
            ("Initialize team evidence state", "ohbs-image state init"),
            ("Inspect runs and health", "ohbs-image report slo"),
            ("Review registered artifacts", "ohbs-image registry list"),
            ("Promote an approved channel", "ohbs-image channel promote --help"),
            ("Plan regional distribution", "ohbs-image distribution plan --help"),
            ("Exercise recovery safely", "ohbs-image dr drill --help"),
        ],
    },
    "consumer": {
        "title": "Image consumer",
        "outcome": "Resolve a deployable image and fail closed on policy violations.",
        "steps": [
            ("Resolve a verified channel", "ohbs-image consumer resolve --help"),
            ("Inspect the channel pointer", "ohbs-image channel resolve --help"),
            ("Verify release evidence", "ohbs-image verify release --help"),
            ("Check downstream impact", "ohbs-image ancestry impact --help"),
        ],
    },
}


def guide_document(role: str | None = None) -> dict[str, Any]:
    """Return the stable machine-readable discovery contract."""
    selected = [role] if role else list(JOURNEYS)
    journeys = []
    for name in selected:
        item = JOURNEYS[name]
        journeys.append({
            "role": name,
            "title": item["title"],
            "outcome": item["outcome"],
            "steps": [
                {"order": index, "goal": goal, "command": command}
                for index, (goal, command) in enumerate(item["steps"], 1)
            ],
        })
    return {
        "schema": GUIDE_SCHEMA,
        "selected_role": role,
        "journeys": journeys,
        "next_action": (journeys[0]["steps"][0]["command"] if role
                        else "ohbs-image guide builder"),
    }


def cmd_guide(args: argparse.Namespace) -> int:
    """Print task-oriented guidance without requiring config or cloud access."""
    document = guide_document(args.role)
    if args.output == "json":
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0
    if args.role is None:
        print("Start with the outcome you own:\n")
        for journey in document["journeys"]:
            first = journey["steps"][0]["command"]
            print(f"  {journey['role']:<10} {journey['title']}")
            print(f"             {journey['outcome']}")
            print(f"             Start: {first}\n")
        print("Show one path: ohbs-image guide builder|security|platform|consumer")
        return 0
    journey = document["journeys"][0]
    print(f"{journey['title']} — {journey['outcome']}\n")
    for step in journey["steps"]:
        print(f"  {step['order']}. {step['goal']}")
        print(f"     $ {step['command']}")
    print(f"\nNext: {document['next_action']}")
    return 0
