from __future__ import annotations

import json

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


def test_viewer_lists_only_authorized_bucket(tmp_path):
    service = _service(tmp_path)
    status, _kind, body = service.dispatch(
        "GET", "/api/v1/artifacts", "Bearer view-token")
    assert status == 200
    assert json.loads(body)["artifacts"][0]["bucket"] == "rhel10"
    status, _, _ = service.dispatch("GET", "/api/v1/artifacts/img-1", "Bearer wrong-bucket")
    assert status == 403


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
