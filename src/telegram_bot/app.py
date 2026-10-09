"""Telegram categorization bot.

Invoked by API Gateway (Telegram webhook: category button presses) or directly /
on a schedule (any non-HTTP event) to send uncategorized expenses to the chat.
"""

import hmac
import html
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from shared.aws_secrets import get_secret_json
from shared.categories import CATEGORIES
from shared.http import json_response
from shared.telegram import TelegramClient, TelegramError

logger = logging.getLogger()

BUTTONS_PER_ROW = 2
CALLBACK_PREFIX = "cat"
MAX_CALLBACK_DATA_BYTES = 64

# Not expenses the user needs to categorize.
EXCLUDED_PRIMARY_CATEGORIES = {"TRANSFER_IN", "TRANSFER_OUT", "INCOME"}
EXCLUDED_DETAILED_CATEGORIES = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}

SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"

_dynamodb = None


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(os.environ["TRANSACTIONS_TABLE"])


def _secret() -> dict[str, Any]:
    return get_secret_json(os.environ["TELEGRAM_SECRET_ARN"])


def _telegram_client() -> TelegramClient:
    return TelegramClient(_secret()["bot_token"])


def _allowed_user_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
    return {int(part) for part in raw.split(",") if part.strip()}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lambda_handler(event, context):
    if "requestContext" not in event:
        return send_uncategorized()
    return handle_webhook(event)


# ----------------------------------------------------------------------
# Sending uncategorized transactions
# ----------------------------------------------------------------------


def is_categorizable(item: dict[str, Any]) -> bool:
    """True for expenses the user should categorize (not transfers, income, card payments)."""
    if Decimal(str(item.get("amount", 0))) <= 0:
        return False
    if item.get("plaid_category_primary") in EXCLUDED_PRIMARY_CATEGORIES:
        return False
    return item.get("plaid_category_detailed") not in EXCLUDED_DETAILED_CATEGORIES


def callback_data(transaction_id: str, category_key: str) -> str:
    return f"{CALLBACK_PREFIX}:{transaction_id}:{category_key}"


def category_keyboard(transaction_id: str) -> dict[str, Any]:
    buttons = [
        {"text": label, "callback_data": callback_data(transaction_id, key)}
        for key, label in CATEGORIES.items()
    ]
    return {
        "inline_keyboard": [
            buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)
        ]
    }


def format_message(item: dict[str, Any]) -> str:
    title = item.get("merchant_name") or item.get("name") or "Unknown transaction"
    amount = Decimal(str(item["amount"]))
    amount_line = f"{amount:,.2f}"
    if item.get("currency"):
        amount_line += f" {item['currency']}"
    lines = [
        f"<b>{html.escape(title)}</b>",
        html.escape(f"{amount_line} · {item['transaction_date']}"),
    ]
    if item.get("status") == "pending":
        lines.append("⏳ Pending")
    account = item.get("account_name")
    if account:
        if item.get("account_mask"):
            account += f" ••{item['account_mask']}"
        lines.append(html.escape(account))
    plaid_category = item.get("plaid_category_detailed") or item.get("plaid_category_primary")
    if plaid_category:
        lines.append(f"Plaid: {html.escape(plaid_category)}")
    return "\n".join(lines)


def _scan_candidates(table) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("expense_category").not_exists()
        & Attr("telegram_message_id").not_exists()
        & Attr("amount").gt(0)
    }
    items: list[dict[str, Any]] = []
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def send_uncategorized() -> dict[str, Any]:
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    max_messages = int(os.environ.get("MAX_MESSAGES_PER_RUN", "20"))
    table = _table()

    candidates = _scan_candidates(table)
    to_send = sorted(
        (item for item in candidates if is_categorizable(item)),
        key=lambda item: (item["transaction_date"], item["transaction_id"]),
    )
    excluded = len(candidates) - len(to_send)

    client = _telegram_client() if to_send else None
    sent = 0
    for item in to_send[:max_messages]:
        transaction_id = item["transaction_id"]
        if any(
            len(callback_data(transaction_id, key).encode()) > MAX_CALLBACK_DATA_BYTES
            for key in CATEGORIES
        ):
            logger.warning(
                "Transaction ID too long for callback data",
                extra={"transaction_id": transaction_id},
            )
            continue
        message = client.send_message(
            chat_id, format_message(item), reply_markup=category_keyboard(transaction_id)
        )
        table.update_item(
            Key={"transaction_id": transaction_id},
            UpdateExpression=(
                "SET telegram_message_id = :m, telegram_chat_id = :c, telegram_sent_at = :now"
            ),
            ExpressionAttributeValues={
                ":m": message["message_id"],
                ":c": message["chat"]["id"],
                ":now": _now(),
            },
        )
        sent += 1

    counts = {
        "candidates": len(candidates),
        "excluded": excluded,
        "sent": sent,
        "remaining": max(len(to_send) - max_messages, 0),
    }
    logger.info("Uncategorized transactions sent", extra=counts)
    return counts


# ----------------------------------------------------------------------
# Webhook (category button presses)
# ----------------------------------------------------------------------


def handle_webhook(event: dict[str, Any]) -> dict[str, Any]:
    # Never log the request body: it may contain financial data.
    request_id = event["requestContext"].get("requestId")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    expected = _secret().get("webhook_secret_token") or ""
    provided = headers.get(SECRET_TOKEN_HEADER) or ""
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("Invalid Telegram secret token", extra={"request_id": request_id})
        return json_response(401, {"ok": False})

    try:
        update = json.loads(event.get("body") or "")
    except ValueError:
        logger.warning("Invalid Telegram update body", extra={"request_id": request_id})
        return json_response(400, {"ok": False})

    callback_query = update.get("callback_query")
    logger.info(
        "Telegram update received",
        extra={
            "request_id": request_id,
            "update_id": update.get("update_id"),
            "is_callback": callback_query is not None,
        },
    )
    if callback_query:
        try:
            handle_callback(callback_query)
        except TelegramError:
            # Return 200 anyway so Telegram does not keep redelivering the update.
            logger.exception("Telegram API call failed", extra={"request_id": request_id})
    return json_response(200, {"ok": True})


def handle_callback(callback_query: dict[str, Any]) -> None:
    client = _telegram_client()
    query_id = callback_query["id"]
    user_id = (callback_query.get("from") or {}).get("id")
    if user_id not in _allowed_user_ids():
        logger.warning("Callback from a user that is not allowed", extra={"user_id": user_id})
        client.answer_callback_query(query_id, "Not allowed")
        return

    parts = (callback_query.get("data") or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[2] not in CATEGORIES:
        client.answer_callback_query(query_id, "Unknown category")
        return
    _, transaction_id, category_key = parts

    item = _set_category(transaction_id, category_key, user_id)
    if item is None:
        client.answer_callback_query(query_id, "Transaction no longer exists")
        return

    label = CATEGORIES[category_key]
    logger.info(
        "Transaction categorized",
        extra={"transaction_id": item["transaction_id"], "expense_category": category_key},
    )
    client.answer_callback_query(query_id, f"Saved: {label}")
    message = callback_query.get("message")
    if message:
        client.edit_message_text(
            message["chat"]["id"],
            message["message_id"],
            f"{format_message(item)}\n\n✅ Category: <b>{html.escape(label)}</b>",
        )


def _set_category(transaction_id: str, category_key: str, user_id: int) -> dict[str, Any] | None:
    """Save the category; return the updated item, or None if the transaction is gone."""
    table = _table()
    item = _update_category(table, transaction_id, category_key, user_id)
    if item is None:
        # A pending transaction gets a new ID once posted; follow pending_transaction_id.
        posted_id = _posted_transaction_id(table, transaction_id)
        if posted_id:
            item = _update_category(table, posted_id, category_key, user_id)
    return item


def _update_category(
    table, transaction_id: str, category_key: str, user_id: int
) -> dict[str, Any] | None:
    try:
        response = table.update_item(
            Key={"transaction_id": transaction_id},
            UpdateExpression=(
                "SET expense_category = :k, categorized_at = :now, categorized_by = :u"
            ),
            ConditionExpression="attribute_exists(transaction_id)",
            ExpressionAttributeValues={":k": category_key, ":now": _now(), ":u": user_id},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return None
    return response["Attributes"]


def _posted_transaction_id(table, pending_transaction_id: str) -> str | None:
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("pending_transaction_id").eq(pending_transaction_id),
        "ProjectionExpression": "transaction_id",
    }
    while True:
        response = table.scan(**kwargs)
        if response.get("Items"):
            return response["Items"][0]["transaction_id"]
        if "LastEvaluatedKey" not in response:
            return None
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
