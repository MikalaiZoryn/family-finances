"""Plaid webhook verification (Plaid-Verification JWT, ES256)."""

import hashlib
import hmac
import logging
import time
from typing import Any

import jwt

from shared.plaid import PlaidClient, PlaidError

logger = logging.getLogger(__name__)

MAX_TOKEN_AGE_SECONDS = 5 * 60

# kid -> JWK, kept for the life of the container. A key with expired_at set is rotated out.
_keys: dict[str, dict[str, Any]] = {}


def verify(client: PlaidClient, headers: dict[str, str], raw_body: bytes) -> bool:
    """True if the request carries a valid Plaid-Verification JWT for this exact body."""
    token = next((v for k, v in headers.items() if k.lower() == "plaid-verification"), None)
    if not token:
        return False
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return False
    if header.get("alg") != "ES256" or not header.get("kid"):
        return False

    jwk = _key(client, header["kid"])
    if jwk is None or jwk.get("expired_at"):
        return False
    try:
        claims = jwt.decode(token, key=jwt.PyJWK(jwk), algorithms=["ES256"])
    except jwt.PyJWTError:
        return False

    iat = claims.get("iat")
    if not isinstance(iat, int | float) or time.time() - iat > MAX_TOKEN_AGE_SECONDS:
        return False
    expected = claims.get("request_body_sha256") or ""
    actual = hashlib.sha256(raw_body).hexdigest()
    return hmac.compare_digest(actual, expected)


def _key(client: PlaidClient, kid: str) -> dict[str, Any] | None:
    if kid not in _keys:
        try:
            _keys[kid] = client.webhook_verification_key_get(kid)["key"]
        except PlaidError as e:
            logger.warning("Plaid webhook key lookup failed", extra={"request_id": e.request_id})
            return None
    return _keys[kid]


def clear_cache() -> None:
    _keys.clear()
