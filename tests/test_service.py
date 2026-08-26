from __future__ import annotations

import json

from ohbs_image._ancestry import link_parent
from ohbs_image._cli import build_parser
from ohbs_image._registry import register_release
from ohbs_image._service import ControlPlane


def _service(tmp_path):
    release = tmp_path / "releases" / "img-1.json"
    release.parent.mkdir(parents=True)
    release.write_text(json.dumps({"image_id": "img-1", "run_id": "run-1",
        "profile": "rhel10", "region": "ap-guangzhou", "state": "approved"}), encoding="utf-8")
    register_release(release, tmp_path / "registry")
    rbac = tmp_path / "rbac.json"
    rbac.write_text(json.dumps({"tokens": {
        "view-token": {"subject": "reader", "roles": ["viewer"], "buckets": ["rhel10"]},
        "promote-token": {"subject": "release", "roles": ["viewer", "promoter"], "buckets": ["rhel10"]},
        "wrong-bucket": {"subject": "other", "roles": ["viewer"], "buckets": ["ubuntu2404"]}}}),
        encoding="utf-8")
    return ControlPlane(tmp_path, rbac)


def test_service_requires_valid_bearer_token(tmp_path):
    service = _service(tmp_path)
    status, _kind, body = service.dispatch("GET", "/api/v1/artifacts", "")
    assert status == 403 and "bearer" in body.decode()


def test_health_is_public(tmp_path):
    service = _service(tmp_path)
    status, _kind, body = service.dispatch("GET", "/healthz", "")
    assert status == 200 and json.loads(body)["status"] == "ok"
    status, _kind, body = service.dispatch("GET", "/readyz", "")
    assert status == 200 and json.loads(body)["status"] == "ready"


def test_rate_limit_returns_stable_problem(tmp_path):
    service = _service(tmp_path)
    service.rate_limit = 1
    status, _, _ = service.dispatch("GET", "/api/v1/artifacts", "Bearer view-token")
    assert status == 200
    status, _, body = service.dispatch("GET", "/api/v1/artifacts", "Bearer view-token")
    assert status == 429
    assert json.loads(body)["error"]["code"] == "rate_limited"


def test_rbac_hot_reload_and_token_expiry(tmp_path):
    service = _service(tmp_path)
    rbac = tmp_path / "rbac.json"
    value = json.loads(rbac.read_text(encoding="utf-8"))
    value["tokens"]["expired"] = {"subject": "old", "roles": ["viewer"],
        "buckets": ["rhel10"], "expires_at": "2020-01-01T00:00:00Z"}
    value["tokens"]["new-token"] = {"subject": "new", "roles": ["viewer"],
        "buckets": ["rhel10"]}
    rbac.write_text(json.dumps(value), encoding="utf-8")
    service._rbac_mtime_ns = -1
    status, _, _ = service.dispatch("GET", "/api/v1/artifacts", "Bearer new-token")
    assert status == 200
    status, _, body = service.dispatch("GET", "/api/v1/artifacts", "Bearer expired")
    assert status == 403 and "expired" in body.decode()


def test_console_assets_are_public_but_contain_no_credentials(tmp_path):
    service = _service(tmp_path)
    status, kind, page = service.dispatch("GET", "/console", "")
    assert status == 200 and kind.startswith("text/html")
    assert b"ohbs-image Control Plane" in page
    assert b"view-token" not in page and b"promote-token" not in page
    status, kind, script = service.dispatch("GET", "/console.js", "")
    assert status == 200 and kind.startswith("text/javascript")
    assert b"localStorage" not in script and b"innerHTML" not in script
    status, kind, style = service.dispatch("GET", "/console.css", "")
    assert status == 200 and kind.startswith("text/css") and b"prefers-reduced-motion" in style


def test_service_default_port_is_8181():
    args = build_parser().parse_args(["serve", "--rbac", "rbac.json"])
    assert args.host == "127.0.0.1"
    assert args.port == 8181


def test_viewer_lists_only_authorized_bucket(tmp_path):
    service = _service(tmp_path)
    status, _kind, body = service.dispatch(
        "GET", "/api/v1/artifacts", "Bearer view-token")
    assert status == 200
    assert json.loads(body)["artifacts"][0]["bucket"] == "rhel10"
    status, _, _ = service.dispatch("GET", "/api/v1/artifacts/img-1", "Bearer wrong-bucket")
    assert status == 403


def test_artifact_list_filters_and_paginates(tmp_path):
    service = _service(tmp_path)
    status, _, body = service.dispatch(
        "GET", "/api/v1/artifacts?status=active&limit=1&offset=0", "Bearer view-token")
    result = json.loads(body)
    assert status == 200 and result["count"] == 1
    assert result["limit"] == 1 and result["artifacts"][0]["artifact_id"] == "img-1"
    status, _, body = service.dispatch(
        "GET", "/api/v1/artifacts?limit=0", "Bearer view-token")
    assert status == 400 and json.loads(body)["error"]["code"] == "invalid_request"


def test_runs_list_show_and_scope(tmp_path):
    service = _service(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run-1.json").write_text(json.dumps({
        "run_id": "run-1", "profile": "rhel10", "status": "ok"}), encoding="utf-8")
    (runs / "run-other.json").write_text(json.dumps({
        "run_id": "run-other", "profile": "ubuntu2404", "status": "ok"}), encoding="utf-8")
    status, _, body = service.dispatch("GET", "/api/v1/runs", "Bearer view-token")
    result = json.loads(body)
    assert status == 200 and result["count"] == 1
    assert result["runs"][0]["run_id"] == "run-1"
    status, _, body = service.dispatch("GET", "/api/v1/runs/run-1", "Bearer view-token")
    assert status == 200 and json.loads(body)["run"]["run_id"] == "run-1"
    status, _, _ = service.dispatch("GET", "/api/v1/runs/run-other", "Bearer view-token")
    assert status == 403


def test_impact_and_rebuild_requests_are_bucket_scoped(tmp_path):
    service = _service(tmp_path)
    release = tmp_path / "releases" / "img-2.json"
    release.write_text(json.dumps({"image_id": "img-2", "run_id": "run-2",
        "profile": "rhel10", "region": "ap-guangzhou", "state": "approved"}),
        encoding="utf-8")
    register_release(release, tmp_path / "registry")
    link_parent("img-2", "img-1", root=tmp_path / "registry")
    status, _, body = service.dispatch(
        "GET", "/api/v1/artifacts/img-1/impact", "Bearer view-token")
    assert status == 200 and json.loads(body)["descendant_count"] == 1
    requests = tmp_path / "registry" / "rebuild_requests"
    requests.mkdir()
    (requests / "one.json").write_text(json.dumps({
        "request_id": "evt-1:img-1", "artifact_id": "img-1", "status": "queued",
        "created_at": "2026-08-26T00:00:00Z"}), encoding="utf-8")
    status, _, body = service.dispatch(
        "GET", "/api/v1/rebuild-requests?status=queued", "Bearer view-token")
    result = json.loads(body)
    assert status == 200 and result["count"] == 1
    assert result["rebuild_requests"][0]["artifact_id"] == "img-1"


def test_promoter_needs_idempotency_key_and_writes_audit(tmp_path):
    service = _service(tmp_path)
    payload = json.dumps({"artifact_id": "img-1", "expected_generation": 0}).encode()
    status, _, _ = service.dispatch("PUT", "/api/v1/channels/rhel10/stable",
                                    "Bearer promote-token", body=payload)
    assert status == 400
    status, _, body = service.dispatch("PUT", "/api/v1/channels/rhel10/stable",
        "Bearer promote-token", body=payload, headers={"Idempotency-Key": "deploy-1"})
    assert status == 200 and json.loads(body)["generation"] == 1
    audit = (tmp_path / "audit" / "service.jsonl").read_text(encoding="utf-8")
    assert "channel.promote" in audit and "release" in audit


def test_viewer_cannot_promote(tmp_path):
    service = _service(tmp_path)
    status, _, _ = service.dispatch("PUT", "/api/v1/channels/rhel10/stable",
        "Bearer view-token", body=b'{"artifact_id":"img-1"}',
        headers={"Idempotency-Key": "deploy-2"})
    assert status == 403
