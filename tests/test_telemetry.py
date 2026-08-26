from __future__ import annotations

import json
from unittest import mock

import pytest

from ohbs_image._telemetry import TraceRecorder, TrendStore, parse_traceparent, push_otlp


def test_nested_spans_preserve_trace_and_parent_relationship(tmp_path):
    recorder = TraceRecorder(tmp_path)
    with recorder.span("root") as root, recorder.span("child") as child:
        child["attributes"]["artifact_id"] = "img-1"
    rows = [json.loads(line) for line in (
        tmp_path / "telemetry" / "traces.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["trace_id"] == rows[1]["trace_id"] == root["trace_id"]
    assert rows[0]["parent_span_id"] == root["span_id"]
    assert rows[1]["parent_span_id"] == ""


def test_error_span_is_recorded_and_re_raised(tmp_path):
    recorder = TraceRecorder(tmp_path)
    with pytest.raises(RuntimeError), recorder.span("failing"):
        raise RuntimeError("boom")
    row = json.loads((tmp_path / "telemetry" / "traces.jsonl").read_text())
    assert row["status"] == "error" and "boom" in row["error"]


def test_traceparent_validation():
    trace = "1" * 32
    parent = "2" * 16
    assert parse_traceparent(f"00-{trace}-{parent}-01") == (trace, parent)
    assert parse_traceparent("00-invalid-parent-01") == ("", "")


def test_otlp_push_uses_signal_endpoint():
    response = mock.MagicMock(status=200)
    response.__enter__.return_value = response
    with mock.patch("urllib.request.urlopen", return_value=response) as opened:
        push_otlp({"resourceSpans": []}, "https://otel.example")
    assert opened.call_args.args[0].full_url == "https://otel.example/v1/traces"


def test_trend_store_records_and_queries_snapshots(tmp_path):
    store = TrendStore(tmp_path / "trends.db")
    store.record({"success_rate": 99}, recorded_at="2026-08-26T00:00:00+00:00")
    store.record({"success_rate": 98}, recorded_at="2026-08-27T00:00:00+00:00")
    rows = store.query(limit=1)
    assert rows == [{"recorded_at": "2026-08-27T00:00:00+00:00",
                     "snapshot": {"success_rate": 98}}]
