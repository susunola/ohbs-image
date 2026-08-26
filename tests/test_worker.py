from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from ohbs_image._worker import (
    WORKER_RESULT_SCHEMA,
    _pipeline_handler,
    claim_request,
    finish_request,
    process_one,
)


def _request(queue, *, status="queued", attempt=0):
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / "request.json"
    path.write_text(json.dumps({
        "request_id": "event-1:img-1", "event_id": "event-1",
        "artifact_id": "img-1", "status": status, "attempt": attempt,
        "created_at": "2026-08-26T00:00:00Z",
    }), encoding="utf-8")
    return path


def _success(_request_doc):
    return {"schema": WORKER_RESULT_SCHEMA, "artifact_id": "img-new",
            "stages": {stage: {"status": "succeeded"} for stage in (
                "build", "policy", "distribute", "promote")}}


def test_worker_completes_strict_end_to_end_result(tmp_path):
    queue = tmp_path / "rebuild_requests"
    path = _request(queue)

    result = process_one(queue, _success, worker_id="worker-a")

    assert result is not None and result["status"] == "succeeded"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["result"]["artifact_id"] == "img-new"
    assert stored["attempt"] == 1 and "lease_expires_at" not in stored


def test_worker_retries_then_dead_letters(tmp_path):
    queue = tmp_path / "rebuild_requests"
    path = _request(queue)
    claimed = claim_request(queue, "worker-a")
    assert claimed is not None
    _, request = claimed
    retry = finish_request(path, request, error="capacity unavailable", max_attempts=2,
                           retry_delay_seconds=0)
    assert retry["status"] == "retry_wait"

    claimed = claim_request(queue, "worker-b")
    assert claimed is not None
    _, request = claimed
    dead = finish_request(path, request, error="capacity unavailable", max_attempts=2)
    assert dead["status"] == "dead_letter"
    assert [event["status"] for event in dead["worker_history"]] == [
        "running", "retry_wait", "running", "dead_letter"]


def test_expired_lease_is_reclaimed_with_new_owner(tmp_path):
    queue = tmp_path / "rebuild_requests"
    path = _request(queue, status="running", attempt=1)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(worker_id="dead-worker", lease_expires_at=(
        datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    path.write_text(json.dumps(doc), encoding="utf-8")

    claimed = claim_request(queue, "recovery-worker")

    assert claimed is not None
    assert claimed[1]["worker_id"] == "recovery-worker"
    assert claimed[1]["attempt"] == 2


def test_invalid_stage_result_is_retryable_failure(tmp_path):
    queue = tmp_path / "rebuild_requests"
    _request(queue)

    result = process_one(queue, lambda _request_doc: {
        "schema": WORKER_RESULT_SCHEMA, "artifact_id": "img-new", "stages": {}},
        worker_id="worker-a")

    assert result is not None and result["status"] == "retry_wait"
    assert "stage build did not succeed" in result["error"]


def test_builtin_pipeline_executes_all_stages_and_propagates_artifact(tmp_path):
    script = tmp_path / "stage.py"
    script.write_text("import json,sys\nrequest=json.load(sys.stdin)\n"
                      "print(json.dumps({'artifact_id': sys.argv[2]}))\n")
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps({"cwd": str(tmp_path), "stages": {
        stage: {"command": ["python3", str(script), stage,
                            "img-new" if stage == "build" else "{artifact_id}"]}
        for stage in ("build", "policy", "distribute", "promote")}}))

    result = _pipeline_handler(pipeline, 10)({
        "request_id": "evt:img-1", "event_id": "evt", "artifact_id": "img-1"})

    assert result["artifact_id"] == "img-new"
    assert all(row["status"] == "succeeded" for row in result["stages"].values())
