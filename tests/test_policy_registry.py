from __future__ import annotations

import json

import pytest

from ohbs_image._policy_registry import (
    list_policies,
    publish_policy,
    resolve_policy,
    revoke_policy,
)


def _bundle(path, version="1", score=95):
    path.write_text(json.dumps({
        "schema": "https://ohbs-image.dev/policy-bundle/v1",
        "policy_id": "organization", "version": version,
        "defaults": {"min_score": score}, "environments": {}, "exceptions": [],
    }), encoding="utf-8")


def test_publish_activate_resolve_and_revoke(tmp_path):
    bundle = tmp_path / "policy.json"
    _bundle(bundle)
    registry = tmp_path / "registry"
    published = publish_policy(bundle, actor="security@example.com", activate=True,
                               root=registry)
    assert published["status"] == "active"
    assert resolve_policy("organization", root=registry)["version"] == "1"
    assert len(list_policies(registry)) == 1
    revoked = revoke_policy("organization", "1", actor="security@example.com",
                            reason="superseded", root=registry)
    assert revoked["status"] == "revoked"
    with pytest.raises(ValueError, match="no active policy"):
        resolve_policy("organization", root=registry)


def test_policy_versions_are_immutable_and_publish_is_idempotent(tmp_path):
    bundle = tmp_path / "policy.json"
    _bundle(bundle)
    registry = tmp_path / "registry"
    first = publish_policy(bundle, actor="security", root=registry)
    assert publish_policy(bundle, actor="security", root=registry) == first
    activated = publish_policy(bundle, actor="security", activate=True, root=registry)
    assert activated["status"] == "active"
    assert resolve_policy("organization", root=registry)["version"] == "1"
    _bundle(bundle, score=80)
    with pytest.raises(ValueError, match="immutable"):
        publish_policy(bundle, actor="security", root=registry)


def test_revoked_pinned_version_fails_closed(tmp_path):
    bundle = tmp_path / "policy.json"
    _bundle(bundle)
    registry = tmp_path / "registry"
    publish_policy(bundle, actor="security", root=registry)
    revoke_policy("organization", "1", actor="security", reason="compromised", root=registry)
    with pytest.raises(ValueError, match="revoked"):
        resolve_policy("organization", "1", root=registry)
