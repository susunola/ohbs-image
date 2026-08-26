from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class IdentityError(ValueError):
    pass


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise IdentityError("OIDC token contains invalid base64") from exc


def verify_oidc_token(token: str, config: dict[str, Any], *,
                      now: int | None = None) -> dict[str, Any]:
    """Verify a short-lived HS256 OIDC token and map claims to local RBAC.

    The shared key is intended for an identity-aware proxy or private issuer and
    must be supplied through the RBAC file. No unsigned token is ever accepted.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise IdentityError("OIDC token must be a signed JWT")
    try:
        header = json.loads(_decode(parts[0]))
        claims = json.loads(_decode(parts[1]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityError("OIDC token JSON is invalid") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise IdentityError("OIDC token structure is invalid")
    algorithm = header.get("alg")
    signed = f"{parts[0]}.{parts[1]}".encode()
    signature = _decode(parts[2])
    if algorithm == "HS256":
        secret = str(config.get("client_secret") or "")
        if len(secret.encode()) < 32:
            raise IdentityError("OIDC client_secret must contain at least 32 bytes")
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            raise IdentityError("OIDC token signature is invalid")
    elif algorithm == "RS256":
        key_id = str(header.get("kid") or "")
        keys = config.get("jwks", {}).get("keys", [])
        key = next((item for item in keys if isinstance(item, dict)
                    and item.get("kid") == key_id and item.get("kty") == "RSA"), None)
        if key is None:
            raise IdentityError("OIDC signing key is unknown")
        try:
            modulus = int.from_bytes(_decode(str(key["n"])), "big")
            exponent = int.from_bytes(_decode(str(key["e"])), "big")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, KeyError, ValueError, TypeError) as exc:
            raise IdentityError("OIDC token signature is invalid") from exc
    else:
        raise IdentityError("OIDC algorithm must be RS256 or HS256")
    current = int(time.time()) if now is None else now
    try:
        issued, expires = int(claims["iat"]), int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityError("OIDC token requires integer iat and exp claims") from exc
    max_ttl = int(config.get("max_token_ttl_seconds", 3600))
    if expires <= current or issued > current + 60:
        raise IdentityError("OIDC token is expired or not yet valid")
    if expires - issued > max_ttl:
        raise IdentityError("OIDC token lifetime exceeds configured maximum")
    if "nbf" in claims and int(claims["nbf"]) > current:
        raise IdentityError("OIDC token is not yet valid")
    if claims.get("iss") != config.get("issuer"):
        raise IdentityError("OIDC issuer mismatch")
    audience = claims.get("aud")
    audiences = {str(audience)} if isinstance(audience, str) else {
        str(item) for item in audience or []}
    if str(config.get("audience") or "") not in audiences:
        raise IdentityError("OIDC audience mismatch")
    revoked = {str(item) for item in config.get("revoked_jti", [])}
    if str(claims.get("jti") or "") in revoked:
        raise IdentityError("OIDC token has been revoked")
    subject = str(claims.get("sub") or "")
    if not subject:
        raise IdentityError("OIDC subject is required")
    groups = {str(item) for item in claims.get("groups", [])}
    roles: set[str] = set()
    buckets: set[str] = set()
    for group, mapping in config.get("group_mappings", {}).items():
        if group in groups and isinstance(mapping, dict):
            roles.update(str(item) for item in mapping.get("roles", []))
            buckets.update(str(item) for item in mapping.get("buckets", []))
    if not roles:
        raise IdentityError("OIDC identity has no mapped roles")
    return {"subject": subject, "roles": sorted(roles), "buckets": sorted(buckets),
            "auth_method": "oidc", "token_id": claims.get("jti", "")}
