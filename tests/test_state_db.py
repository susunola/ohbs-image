from __future__ import annotations

import json

import pytest

from ohbs_image._state_db import StateDatabase
from ohbs_image._worker import WORKER_RESULT_SCHEMA, process_one_db


def _success(_request):
    return {"schema": WORKER_RESULT_SCHEMA, "artifact_id": "new-image",
            "stages": {name: {"status": "succeeded"} for name in
                       ("build", "policy", "distribute", "promote")}}


def test_migrate_verify_backup_and_reversible_export(tmp_path):
    root = tmp_path / "legacy"
    request_dir = root / "registry" / "rebuild_requests"
    request_dir.mkdir(parents=True)
    request = {"request_id": "evt:img", "artifact_id": "img", "status": "queued"}
    (request_dir / "one.json").write_text(json.dumps(request), encoding="utf-8")
    (root / "lineage.jsonl").write_text('{"artifact_id":"img"}\n', encoding="utf-8")
    database = StateDatabase(tmp_path / "state.db")

    assert database.import_tree(root) == {"objects": 2, "rebuild_requests": 1}
    assert database.verify()["valid"] is True
    database.backup(tmp_path / "backup.db")
    assert StateDatabase(tmp_path / "backup.db").verify()["objects"] == 2
    assert database.export_tree(tmp_path / "restored") == 2
    assert (tmp_path / "restored" / "lineage.jsonl").read_text() == '{"artifact_id":"img"}\n'
    with pytest.raises(FileExistsError):
        database.export_tree(tmp_path / "restored")


def test_transactional_worker_claim_and_completion(tmp_path):
    root = tmp_path / "legacy"
    queue = root / "registry" / "rebuild_requests"
    queue.mkdir(parents=True)
    (queue / "one.json").write_text(json.dumps(
        {"request_id": "evt:img", "artifact_id": "img", "status": "queued"}),
        encoding="utf-8")
    database = StateDatabase(tmp_path / "state.db")
    database.import_tree(root)

    result = process_one_db(database, _success, worker_id="worker-a")

    assert result is not None and result["status"] == "succeeded"
    assert database.claim("worker-b") is None


def test_transaction_rolls_back_on_failure(tmp_path):
    database = StateDatabase(tmp_path / "state.db")
    database.initialize()
    with pytest.raises(RuntimeError), database.transaction() as connection:
        connection.execute("INSERT INTO metadata VALUES('temporary','yes')")
        raise RuntimeError("abort")
    with database.connect() as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='temporary'").fetchone() is None
