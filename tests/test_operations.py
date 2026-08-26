from __future__ import annotations

import json

import pytest

from ohbs_image._channels import promote_channel
from ohbs_image._distribution import record_replica
from ohbs_image._operations import (
    StaleFencingTokenError,
    fenced_operation,
    verify_fencing_token,
)
from ohbs_image._registry import register_release


def _artifact(root, artifact_id="img-1"):
    release = root.parent / "releases" / f"{artifact_id}.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(json.dumps({
        "image_id": artifact_id, "run_id": "v1", "profile": "rhel10",
        "region": "ap-guangzhou", "state": "approved",
    }), encoding="utf-8")
    return register_release(release, root)


def test_channel_operation_replays_without_advancing_generation(tmp_path):
    root = tmp_path / "registry"
    _artifact(root)
    first = promote_channel("rhel10", "stable", "img-1", operation_id="deploy-42", root=root)
    replay = promote_channel("rhel10", "stable", "img-1", operation_id="deploy-42", root=root)
    assert replay == first
    assert first["generation"] == 1
    assert first["operation_id"] == "deploy-42"
    assert first["fencing_token"] == 1


def test_different_operation_advances_fence_and_rejects_old_token(tmp_path):
    root = tmp_path / "registry"
    with fenced_operation("channel:rhel10/stable", "one", root=root) as first:
        first["result"] = {"ok": True}
    with fenced_operation("channel:rhel10/stable", "two", root=root) as second:
        assert second["token"] == 2
        with pytest.raises(StaleFencingTokenError, match="stale fencing token 1"):
            verify_fencing_token("channel:rhel10/stable", 1, root)
        second["result"] = {"ok": True}


def test_replica_record_operation_is_idempotent(tmp_path):
    root = tmp_path / "registry"
    _artifact(root)
    first = record_replica("img-1", "ap-shanghai", "ami-copy",
                           operation_id="copy-job-7", root=root)
    replay = record_replica("img-1", "ap-shanghai", "ami-copy",
                            operation_id="copy-job-7", root=root)
    assert replay == first
    assert replay["replicas"]["ap-shanghai"]["replica_id"] == "ami-copy"


def test_operation_id_and_scope_are_validated(tmp_path):
    with pytest.raises(ValueError, match="operation_id"), \
            fenced_operation("channel:rhel10/stable", "", root=tmp_path):
        pass
    with pytest.raises(ValueError, match="scope"), \
            fenced_operation("../escape", "one", root=tmp_path):
        pass
