"""Minimal Telegram Bot API client over HTTPS (standard library only)."""

import json
import time
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "https://api.telegram.org"
MAX_RETRY_AFTER_SECONDS = 30


class TelegramError(Exception):
    def __init__(
        self,
        description: str | None,
        error_code: int | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(f"{error_code}: {description}")
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, bot_token: str, timeout: float = 10):
        self._base_url = f"{BASE_URL}/bot{bot_token}"
        self._timeout = timeout

    def _post(self, method: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read())
            except ValueError:
                payload = {"ok": False, "description": str(e), "error_code": e.code}
        if not payload.get("ok"):
            raise TelegramError(
                payload.get("description"),
                payload.get("error_code"),
                (payload.get("parameters") or {}).get("retry_after"),
            )
        return payload.get("result")

    def send_message(
        self, chat_id: int | str, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            body["reply_markup"] = reply_markup
        try:
            return self._post("sendMessage", body)
        except TelegramError as e:
            # Rate limited: wait as instructed and retry once.
            if e.error_code != 429 or not e.retry_after:
                raise
            time.sleep(min(e.retry_after, MAX_RETRY_AFTER_SECONDS))
            return self._post("sendMessage", body)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        return self._post("answerCallbackQuery", body)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            body["reply_markup"] = reply_markup
        return self._post("editMessageText", body)
