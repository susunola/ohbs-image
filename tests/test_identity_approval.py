from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ohbs_image._approvals import approve, consume_approval, create_approval
from ohbs_image._identity import IdentityError, verify_oidc_token
from ohbs_image._service import ControlPlane


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode() \
        ).decode().rstrip("=")


def _token(secret, claims):
    header, payload = _encode({"alg": "HS256", "typ": "JWT"}), _encode(claims)
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def _oidc(secret):
    return {"issuer": "https://id.example", "audience": "ohbs-control-plane",
            "client_secret": secret, "max_token_ttl_seconds": 900,
            "group_mappings": {"platform-readers": {
                "roles": ["viewer"], "buckets": ["rhel10"]}}}


def test_oidc_validates_signature_lifetime_audience_and_group_mapping():
    secret = "a-production-grade-shared-secret-32bytes"
    now = int(time.time())
    token = _token(secret, {"iss": "https://id.example", "aud": "ohbs-control-plane",
        "sub": "alice@example.com", "groups": ["platform-readers"], "iat": now,
        "exp": now + 600, "jti": "token-1"})
    principal = verify_oidc_token(token, _oidc(secret), now=now)
    assert principal["subject"] == "alice@example.com"
    assert principal["roles"] == ["viewer"] and principal["auth_method"] == "oidc"
    with pytest.raises(IdentityError, match="signature"):
        verify_oidc_token(token, _oidc("different-production-secret-32bytes"), now=now)


def test_oidc_rejects_long_lived_and_revoked_tokens():
    secret = "a-production-grade-shared-secret-32bytes"
    now = int(time.time())
    claims = {"iss": "https://id.example", "aud": "ohbs-control-plane", "sub": "alice",
              "groups": ["platform-readers"], "iat": now, "exp": now + 3600, "jti": "bad"}
    with pytest.raises(IdentityError, match="lifetime"):
        verify_oidc_token(_token(secret, claims), _oidc(secret), now=now)
    claims["exp"] = now + 600
    config = _oidc(secret)
    config["revoked_jti"] = ["bad"]
    with pytest.raises(IdentityError, match="revoked"):
        verify_oidc_token(_token(secret, claims), config, now=now)


def test_oidc_rs256_verifies_static_jwks():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    b64 = lambda value: base64.urlsafe_b64encode(  # noqa: E731
        value.to_bytes((value.bit_length() + 7) // 8, "big")).decode().rstrip("=")
    now = int(time.time())
    claims = {"iss": "https://id.example", "aud": "ohbs-control-plane", "sub": "alice",
              "groups": ["platform-readers"], "iat": now, "exp": now + 600, "jti": "rsa-1"}
    header, payload = _encode({"alg": "RS256", "kid": "key-1"}), _encode(claims)
    signature = private.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    token = f"{header}.{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"
    config = _oidc("")
    config["jwks"] = {"keys": [{"kid": "key-1", "kty": "RSA",
                                  "n": b64(numbers.n), "e": b64(numbers.e)}]}
    assert verify_oidc_token(token, config, now=now)["subject"] == "alice"


def test_two_person_approval_is_bound_single_use_and_separated(tmp_path):
    payload = {"artifact_id": "img-1", "expected_generation": 0}
    request = create_approval(tmp_path, requester="release", action="channel.promote",
                              resource="rhel10/stable", payload=payload, required=2)
    with pytest.raises(ValueError, match="own"):
        approve(tmp_path, request["approval_id"], approver="release")
    assert approve(tmp_path, request["approval_id"], approver="security-a")["status"] == "pending"
    assert approve(tmp_path, request["approval_id"], approver="security-b")["status"] == "approved"
    consumed = consume_approval(tmp_path, request["approval_id"], action="channel.promote",
                                resource="rhel10/stable", payload=payload)
    assert consumed["status"] == "consumed"
    with pytest.raises(ValueError, match="quorum"):
        consume_approval(tmp_path, request["approval_id"], action="channel.promote",
                         resource="rhel10/stable", payload=payload)


def test_control_plane_oidc_and_approval_api(tmp_path):
    secret = "a-production-grade-shared-secret-32bytes"
    now = int(time.time())
    config = _oidc(secret)
    config["group_mappings"].update({
        "release": {"roles": ["promoter"], "buckets": ["rhel10"]},
        "security": {"roles": ["approver"], "buckets": ["rhel10"]}})
    rbac = tmp_path / "rbac.json"
    rbac.write_text(json.dumps({"oidc": config, "approvals": {
        "required_channels": ["stable"], "minimum_approvals": 2}}), encoding="utf-8")
    service = ControlPlane(tmp_path, rbac)

    def bearer(subject, group, jti):
        return "Bearer " + _token(secret, {"iss": config["issuer"], "aud": config["audience"],
            "sub": subject, "groups": [group], "iat": now, "exp": now + 600, "jti": jti})

    operation = {"artifact_id": "img-1", "expected_generation": 0}
    status, _, body = service.dispatch("POST", "/api/v1/approvals", bearer("release", "release", "r"),
        body=json.dumps({"resource": "rhel10/stable", "payload": operation}).encode())
    assert status == 201
    approval_id = json.loads(body)["approval_id"]
    for subject in ("security-a", "security-b"):
        status, _, body = service.dispatch("POST", f"/api/v1/approvals/{approval_id}/approve",
                                           bearer(subject, "security", subject))
        assert status == 200
    assert json.loads(body)["status"] == "approved"
