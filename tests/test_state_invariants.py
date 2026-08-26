"""Deterministic model-style checks for safety invariants across state transitions."""

from __future__ import annotations

import itertools
import json

import pytest

from ohbs_image._channels import promote_channel, resolve_channel
from ohbs_image._registry import change_artifact_status, register_release


def _artifact(root, image_id: str) -> None:
    release = root / "releases" / f"{image_id}.json"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(json.dumps({
        "image_id": image_id,
        "run_id": f"run-{image_id}",
        "profile": "rhel10",
        "region": "ap-guangzhou",
        "state": "approved",
        "approved_at": "2026-08-26T00:00:00Z",
    }), encoding="utf-8")
    register_release(release, root / "registry")


@pytest.mark.parametrize("order", tuple(itertools.permutations(("img-a", "img-b", "img-c"))))
def test_channel_model_preserves_monotonic_generation_and_latest_target(tmp_path, order) -> None:
    root = tmp_path / "state"
    for image_id in order:
        _artifact(root, image_id)
    for generation, image_id in enumerate(order, 1):
        pointer = promote_channel(
            "rhel10", "stable", image_id, expected_generation=generation - 1,
            root=root / "registry",
        )
        assert pointer["generation"] == generation
        assert pointer["artifact_id"] == image_id
        assert pointer["previous_artifact_id"] == (order[generation - 2] if generation > 1 else None)
        assert resolve_channel("rhel10", "stable", root / "registry")["artifact"]["artifact_id"] == image_id


def test_failed_channel_write_preserves_previous_pointer_and_releases_lock(tmp_path, monkeypatch) -> None:
    root = tmp_path / "state"
    _artifact(root, "img-a")
    _artifact(root, "img-b")
    promote_channel("rhel10", "stable", "img-a", root=root / "registry")

    def fail_write(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr("ohbs_image._channels._atomic_write_bytes", fail_write)
    with pytest.raises(OSError, match="injected disk failure"):
        promote_channel("rhel10", "stable", "img-b", expected_generation=1, root=root / "registry")
    assert resolve_channel("rhel10", "stable", root / "registry")["artifact"]["artifact_id"] == "img-a"
    assert not (root / "registry" / "channels" / "rhel10" / "stable.json.lock").exists()


@pytest.mark.parametrize("intermediate", (None, "quarantined"))
def test_revocation_is_terminal_across_valid_transition_paths(tmp_path, intermediate) -> None:
    root = tmp_path / "state"
    _artifact(root, "img-a")
    if intermediate:
        change_artifact_status("img-a", intermediate, actor="security", reason="investigate",
                               root=root / "registry")
    change_artifact_status("img-a", "revoked", actor="security", reason="confirmed",
                           root=root / "registry")
    with pytest.raises(ValueError, match="permanently revoked"):
        change_artifact_status("img-a", "quarantined", actor="ops", reason="unsafe retry",
                               root=root / "registry")
