import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from shared import plaid_webhook
from shared.plaid import PlaidError

BODY = b'{"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE"}'
KID = "key-1"


@pytest.fixture
def signing_key():
    return ec.generate_private_key(ec.SECP256R1())


class FakePlaid:
    def __init__(self, key, expired_at=None):
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
        self.jwk = {**jwk, "alg": "ES256", "kid": KID, "use": "sig", "expired_at": expired_at}
        self.calls = 0

    def webhook_verification_key_get(self, key_id):
        self.calls += 1
        if key_id != KID:
            raise PlaidError("INVALID_INPUT", "INVALID_WEBHOOK_VERIFICATION_KEY_ID", "no", "r1")
        return {"key": self.jwk}


@pytest.fixture(autouse=True)
def clear_cache():
    plaid_webhook.clear_cache()
    yield
    plaid_webhook.clear_cache()


def sign(key, body=BODY, iat=None, alg="ES256", kid=KID):
    claims = {"iat": int(time.time()) if iat is None else iat,
              "request_body_sha256": hashlib.sha256(body).hexdigest()}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def test_valid_signature(signing_key):
    client = FakePlaid(signing_key)
    headers = {"Plaid-Verification": sign(signing_key)}  # header name is case-insensitive

    assert plaid_webhook.verify(client, headers, BODY)
    assert plaid_webhook.verify(client, headers, BODY)
    assert client.calls == 1  # key cached by kid


def test_missing_header():
    assert not plaid_webhook.verify(None, {}, BODY)


def test_tampered_body(signing_key):
    headers = {"plaid-verification": sign(signing_key)}

    assert not plaid_webhook.verify(FakePlaid(signing_key), headers, BODY + b" ")


def test_stale_token(signing_key):
    headers = {"plaid-verification": sign(signing_key, iat=int(time.time()) - 301)}

    assert not plaid_webhook.verify(FakePlaid(signing_key), headers, BODY)


def test_wrong_signing_key(signing_key):
    other = ec.generate_private_key(ec.SECP256R1())
    headers = {"plaid-verification": sign(other)}

    assert not plaid_webhook.verify(FakePlaid(signing_key), headers, BODY)


def test_non_es256_alg_rejected(signing_key):
    token = jwt.encode(
        {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(BODY).hexdigest()},
        "s" * 32,
        algorithm="HS256",
        headers={"kid": KID},
    )

    assert not plaid_webhook.verify(FakePlaid(signing_key), {"plaid-verification": token}, BODY)


def test_expired_key_rejected(signing_key):
    client = FakePlaid(signing_key, expired_at=1)

    assert not plaid_webhook.verify(client, {"plaid-verification": sign(signing_key)}, BODY)


def test_unknown_kid_rejected(signing_key):
    headers = {"plaid-verification": sign(signing_key, kid="other")}

    assert not plaid_webhook.verify(FakePlaid(signing_key), headers, BODY)


def test_garbage_token():
    assert not plaid_webhook.verify(None, {"plaid-verification": "not-a-jwt"}, BODY)
