from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from ohbs_image._distribution import share_artifact
from ohbs_image._distribution_controller import DistributionQueue, propagation_slo
from ohbs_image._registry import register_release


def _artifact(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({"image_id": "img-1", "run_id": "run-1",
        "profile": "rhel10", "region": "ap-guangzhou", "state": "approved"}))
    register_release(release, tmp_path / "registry")


def test_queue_is_deduplicated_and_claim_is_lease_fenced(tmp_path):
    queue = DistributionQueue(tmp_path / "distribution.db")
    first = queue.enqueue("img-1", "ap-shanghai")
    assert queue.enqueue("img-1", "ap-shanghai")["job_id"] == first["job_id"]
    claimed = queue.claim("worker-a", global_limit=1, account_limit=1, region_limit=1)
    assert claimed is not None and claimed["attempt"] == 1
    assert queue.claim("worker-b", global_limit=1, account_limit=1, region_limit=1) is None
    finished = queue.finish(claimed, result={"started": []})
    assert finished["status"] == "succeeded"


def test_queue_retry_then_dead_letter(tmp_path):
    queue = DistributionQueue(tmp_path / "distribution.db")
    queue.enqueue("img-1", "ap-shanghai")
    first = queue.claim("worker-a", global_limit=2, account_limit=2, region_limit=2)
    assert first is not None
    assert queue.finish(first, error="quota", max_attempts=2)["status"] == "queued"
    second = queue.claim("worker-b", global_limit=2, account_limit=2, region_limit=2)
    assert second is not None
    assert queue.finish(second, error="quota", max_attempts=2)["status"] == "dead_letter"


def test_cross_account_share_is_dry_run_unless_applied(tmp_path):
    _artifact(tmp_path)
    called = False

    def api(*_args):
        nonlocal called
        called = True
        return {"Response": {"RequestId": "share-1"}}

    dry = share_artifact("img-1", "103849387508", root=tmp_path / "registry", api=api)
    assert dry["mode"] == "dry-run" and called is False
    result = share_artifact("img-1", "103849387508", apply=True,
        root=tmp_path / "registry", api=api, secret_id="sid", secret_key="key")
    assert result["request_id"] == "share-1" and called is True


def test_propagation_slo_detects_overdue_jobs(tmp_path):
    queue = DistributionQueue(tmp_path / "distribution.db")
    queue.enqueue("img-1", "ap-shanghai")
    future = datetime.now(UTC) + timedelta(minutes=31)
    result = propagation_slo(queue, target_minutes=30, now=future)
    assert result["compliant"] is False and result["breached"] == 1
