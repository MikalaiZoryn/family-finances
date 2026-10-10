import io
import json
import urllib.error

import pytest

from shared import plaid
from shared.plaid import PlaidClient, PlaidError


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_transactions_sync_posts_credentials_and_cursor(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return FakeResponse(json.dumps({"next_cursor": "c2"}).encode())

    monkeypatch.setattr(plaid.urllib.request, "urlopen", fake_urlopen)

    result = PlaidClient("id", "secret").transactions_sync("token", "c1", days_requested=30)

    assert result == {"next_cursor": "c2"}
    assert captured["url"] == "https://sandbox.plaid.com/transactions/sync"
    assert captured["body"] == {
        "client_id": "id",
        "secret": "secret",
        "access_token": "token",
        "count": 500,
        "cursor": "c1",
        "options": {"days_requested": 30},
    }


def test_error_response_raises_plaid_error(monkeypatch):
    error_body = {
        "error_type": "TRANSACTIONS_ERROR",
        "error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
        "error_message": "changed",
        "request_id": "req-1",
    }

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps(error_body).encode())
        )

    monkeypatch.setattr(plaid.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(PlaidError) as exc:
        PlaidClient("id", "secret").transactions_sync("token")

    assert exc.value.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
    assert exc.value.request_id == "req-1"
    assert exc.value.status == 400


def test_unknown_env_is_rejected():
    with pytest.raises(ValueError):
        PlaidClient("id", "secret", "development")


def capture_post(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return FakeResponse(b"{}")

    monkeypatch.setattr(plaid.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_link_token_create_new_item(monkeypatch):
    captured = capture_post(monkeypatch)

    PlaidClient("id", "secret", "production").link_token_create(
        "family", "Family Finances", "https://hook", products=["transactions"], days_requested=30
    )

    assert captured["url"] == "https://production.plaid.com/link/token/create"
    body = captured["body"]
    assert body["products"] == ["transactions"]
    assert body["transactions"] == {"days_requested": 30}
    assert body["hosted_link"] == {}
    assert body["webhook"] == "https://hook"
    assert body["country_codes"] == ["US"]
    assert "access_token" not in body


def test_link_token_create_update_mode(monkeypatch):
    captured = capture_post(monkeypatch)

    PlaidClient("id", "secret").link_token_create(
        "family", "Family Finances", "https://hook", access_token="access-token"
    )

    body = captured["body"]
    assert body["access_token"] == "access-token"
    assert body["update"] == {"account_selection_enabled": True}
    assert "products" not in body
    assert "transactions" not in body
