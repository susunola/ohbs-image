from __future__ import annotations

import json

from ohbs_image._cve_feed import sync_osv
from ohbs_image._registry import register_release


def test_osv_feed_dry_run_and_persisted_deduplication(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    release.parent.mkdir()
    release.write_text(json.dumps({"image_id": "img-1", "run_id": "run-1",
        "profile": "rhel10", "region": "ap-guangzhou", "state": "approved"}))
    register_release(release, tmp_path / "registry")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"artifacts": [{"artifact_id": "img-1",
        "packages": [{"purl": "pkg:rpm/redhat/openssl@3.0"}]}]}))
    state = tmp_path / "feed-state.json"

    def fetch(_url, payload, _timeout):
        assert payload["queries"][0]["package"]["purl"].startswith("pkg:rpm/")
        return {"results": [{"vulns": [{"id": "CVE-2026-0042"}]}]}

    dry_run = sync_osv(inventory, state, fetch=fetch, root=tmp_path / "registry")
    assert dry_run["new_count"] == 1 and not state.exists()
    applied = sync_osv(inventory, state, fetch=fetch, apply=True,
                       root=tmp_path / "registry")
    assert applied["new_count"] == 1 and state.is_file()
    assert len(list((tmp_path / "registry" / "rebuild_requests").glob("*.json"))) == 1
    replay = sync_osv(inventory, state, fetch=fetch, apply=True,
                      root=tmp_path / "registry")
    assert replay["new_count"] == 0


def test_osv_feed_fails_closed_on_misaligned_response(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"artifacts": [{"artifact_id": "img-1",
        "packages": [{"purl": "pkg:pypi/example@1"}]}]}))
    try:
        sync_osv(inventory, tmp_path / "state.json", fetch=lambda *_: {"results": []})
    except ValueError as exc:
        assert "does not match inventory" in str(exc)
    else:
        raise AssertionError("expected fail-closed response validation")


def test_osv_feed_follows_pagination_and_rejects_tampered_cursor(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    release.parent.mkdir()
    release.write_text(json.dumps({"image_id": "img-1", "run_id": "run-1",
        "profile": "rhel10", "region": "ap-guangzhou", "state": "approved"}))
    register_release(release, tmp_path / "registry")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"artifacts": [{"artifact_id": "img-1",
        "packages": [{"purl": "pkg:pypi/example@1"}]}]}))
    calls = 0

    def fetch(_url, payload, _timeout):
        nonlocal calls
        calls += 1
        if "page_token" not in payload["queries"][0]:
            return {"results": [{"vulns": [{"id": "CVE-1"}],
                                  "next_page_token": "next"}]}
        return {"results": [{"vulns": [{"id": "CVE-2"}]}]}

    state = tmp_path / "state.json"
    result = sync_osv(inventory, state, apply=True, fetch=fetch,
                      root=tmp_path / "registry")
    assert calls == 2 and result["new_count"] == 2
    value = json.loads(state.read_text())
    value["seen"] = []
    state.write_text(json.dumps(value))
    try:
        sync_osv(inventory, state, fetch=fetch)
    except ValueError as exc:
        assert "integrity verification" in str(exc)
    else:
        raise AssertionError("expected tampered cursor rejection")
