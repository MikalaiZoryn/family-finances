"""Minimal Plaid API client over HTTPS (standard library only)."""

import json
import urllib.error
import urllib.request
from typing import Any

BASE_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


class PlaidError(Exception):
    def __init__(
        self,
        error_type: str | None,
        error_code: str | None,
        error_message: str | None,
        request_id: str | None = None,
        status: int | None = None,
    ):
        super().__init__(f"{error_type}/{error_code}: {error_message} (request_id={request_id})")
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = error_message
        self.request_id = request_id
        self.status = status


class PlaidClient:
    def __init__(self, client_id: str, secret: str, env: str = "sandbox", timeout: float = 15):
        if env not in BASE_URLS:
            raise ValueError(f"Unknown Plaid environment: {env}")
        self._client_id = client_id
        self._secret = secret
        self._base_url = BASE_URLS[env]
        self._timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {"client_id": self._client_id, "secret": self._secret, **body}
        request = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except ValueError:
                err = {}
            raise PlaidError(
                err.get("error_type"),
                err.get("error_code"),
                err.get("error_message") or str(e),
                err.get("request_id"),
                e.code,
            ) from None

    # Transactions
    def transactions_sync(
        self,
        access_token: str,
        cursor: str | None = None,
        count: int = 500,
        days_requested: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"access_token": access_token, "count": count}
        if cursor:
            body["cursor"] = cursor
        if days_requested:
            body["options"] = {"days_requested": days_requested}
        return self._post("/transactions/sync", body)

    def transactions_refresh(self, access_token: str) -> dict[str, Any]:
        return self._post("/transactions/refresh", {"access_token": access_token})

    # Link
    def link_token_create(
        self,
        client_user_id: str,
        client_name: str,
        webhook: str,
        products: list[str] | None = None,
        access_token: str | None = None,
        days_requested: int | None = None,
        country_codes: tuple[str, ...] = ("US",),
    ) -> dict[str, Any]:
        """Create a Hosted Link token; with access_token it opens update mode instead."""
        body: dict[str, Any] = {
            "user": {"client_user_id": client_user_id},
            "client_name": client_name,
            "country_codes": list(country_codes),
            "language": "en",
            "webhook": webhook,
            "hosted_link": {},
        }
        if access_token:
            # Update mode: no products; let the user add or remove accounts too.
            body["access_token"] = access_token
            body["update"] = {"account_selection_enabled": True}
        else:
            body["products"] = products or []
            if days_requested:
                body["transactions"] = {"days_requested": days_requested}
        return self._post("/link/token/create", body)

    def link_token_get(self, link_token: str) -> dict[str, Any]:
        return self._post("/link/token/get", {"link_token": link_token})

    # Items
    def item_public_token_exchange(self, public_token: str) -> dict[str, Any]:
        return self._post("/item/public_token/exchange", {"public_token": public_token})

    def item_get(self, access_token: str) -> dict[str, Any]:
        return self._post("/item/get", {"access_token": access_token})

    def item_remove(self, access_token: str) -> dict[str, Any]:
        return self._post("/item/remove", {"access_token": access_token})

    def institutions_get_by_id(
        self, institution_id: str, country_codes: tuple[str, ...] = ("US",)
    ) -> dict[str, Any]:
        return self._post(
            "/institutions/get_by_id",
            {"institution_id": institution_id, "country_codes": list(country_codes)},
        )

    # Webhooks
    def webhook_verification_key_get(self, key_id: str) -> dict[str, Any]:
        return self._post("/webhook_verification_key/get", {"key_id": key_id})

    # Sandbox only
    def sandbox_public_token_create(
        self,
        institution_id: str,
        products: list[str],
        webhook: str | None = None,
        override_username: str | None = None,
        days_requested: int | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if webhook:
            options["webhook"] = webhook
        if override_username:
            options["override_username"] = override_username
            options["override_password"] = "pass_good"
        if days_requested:
            options["transactions"] = {"days_requested": days_requested}
        return self._post(
            "/sandbox/public_token/create",
            {"institution_id": institution_id, "initial_products": products, "options": options},
        )

    def sandbox_item_reset_login(self, access_token: str) -> dict[str, Any]:
        return self._post("/sandbox/item/reset_login", {"access_token": access_token})

    def sandbox_item_fire_webhook(
        self, access_token: str, webhook_code: str = "SYNC_UPDATES_AVAILABLE"
    ) -> dict[str, Any]:
        return self._post(
            "/sandbox/item/fire_webhook",
            {
                "access_token": access_token,
                "webhook_type": "TRANSACTIONS",
                "webhook_code": webhook_code,
            },
        )
