from __future__ import annotations

import json

from ohbs_image import build_parser
from ohbs_image._guide import GUIDE_SCHEMA, JOURNEYS, cmd_guide, guide_document


def test_every_journey_has_ordered_actionable_steps() -> None:
    document = guide_document()
    assert document["schema"] == GUIDE_SCHEMA
    assert {item["role"] for item in document["journeys"]} == set(JOURNEYS)
    for journey in document["journeys"]:
        assert journey["outcome"]
        assert [step["order"] for step in journey["steps"]] == list(
            range(1, len(journey["steps"]) + 1))
        assert all(step["command"].startswith("ohbs-image ")
                   for step in journey["steps"])


def test_guide_json_is_stable_and_scoped(capsys) -> None:
    args = build_parser().parse_args(["guide", "security", "--output", "json"])
    assert cmd_guide(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == GUIDE_SCHEMA
    assert payload["selected_role"] == "security"
    assert [item["role"] for item in payload["journeys"]] == ["security"]
    assert payload["next_action"] == "ohbs-image scan --html report.html"


def test_guide_overview_points_to_role_specific_help(capsys) -> None:
    args = build_parser().parse_args(["guide"])
    assert cmd_guide(args) == 0
    text = capsys.readouterr().out
    for role in JOURNEYS:
        assert role in text
    assert "ohbs-image guide builder|security|platform|consumer" in text


def test_help_puts_discovery_first() -> None:
    text = build_parser().format_help()
    assert text.index("start here:") < text.index("build lifecycle:")
    start = text[text.index("start here:"):text.index("build lifecycle:")]
    for command in ("guide", "try", "launch", "quickstart"):
        assert f"    {command}" in start
