from __future__ import annotations

import argparse
import json

import pytest

from ohbs_image._run_events import append_run_event, cmd_run_events, read_run_events, verify_event_chain


def test_event_chain_and_same_state_phase_updates(tmp_path):
    run_id = "11111111-1111-1111-1111-111111111111"
    append_run_event(run_id, "CREATED", phase="created", root=tmp_path)
    append_run_event(run_id, "DIAGNOSING", phase="doctor", root=tmp_path)
    append_run_event(run_id, "DIAGNOSING", phase="doctor-cloud", root=tmp_path)
    append_run_event(run_id, "PLANNED", phase="plan", root=tmp_path)

    events = read_run_events(run_id, tmp_path)
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert events[1]["previous_hash"] == events[0]["event_hash"]
    assert verify_event_chain(run_id, tmp_path) == []


def test_implicit_created_and_illegal_transition(tmp_path):
    run_id = "22222222-2222-2222-2222-222222222222"
    append_run_event(run_id, "BUILDING", root=tmp_path)
    assert [event["to"] for event in read_run_events(run_id, tmp_path)] == ["CREATED", "BUILDING"]
    with pytest.raises(ValueError, match="illegal run transition"):
        append_run_event(run_id, "DIAGNOSING", root=tmp_path)


def test_mutation_is_detected(tmp_path):
    run_id = "33333333-3333-3333-3333-333333333333"
    append_run_event(run_id, "CREATED", root=tmp_path)
    append_run_event(run_id, "READY", root=tmp_path)
    path = tmp_path / "events" / f"{run_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])
    event["phase"] = "tampered"
    lines[0] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert any("hash mismatch" in item for item in verify_event_chain(run_id, tmp_path))


def test_events_command_json(tmp_path, monkeypatch, capsys):
    run_id = "44444444-4444-4444-4444-444444444444"
    append_run_event(run_id, "CREATED", root=tmp_path)
    monkeypatch.setattr("ohbs_image._run_events._lineage_path", lambda: tmp_path / "lineage.jsonl")
    assert cmd_run_events(argparse.Namespace(run_id=run_id, output="json")) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
