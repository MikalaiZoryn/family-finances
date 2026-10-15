"""Telegram categorization bot.

Invoked by API Gateway (Telegram webhook: category button presses and CSV files posted
to the chat) or directly (any non-HTTP event) to send uncategorized expenses to the
chat. New expenses are normally sent by the Plaid sync and the Apple Card import right
away; the direct invoke is a fallback sweep.
"""

import hmac
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from shared.aws_secrets import get_secret_json
from shared.categories import CATEGORIES
from shared.expense_messages import (
    CALLBACK_PREFIX,
    CANCEL_PREFIX,
    CHANGE_PREFIX,
    categorized_keyboard,
    category_keyboard,
    format_categorized,
    format_message,
    is_categorizable,
    send_expense,
)
from shared.http import json_response
from shared.telegram import TelegramClient, TelegramError

logger = logging.getLogger()

SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"
# Apple Card exports are a few KB per month; anything much larger is not one.
MAX_CSV_BYTES = 1_000_000

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


def _is_allowed(user_id: Any) -> bool:
    return user_id in _allowed_user_ids()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lambda_handler(event, context):
    if "requestContext" not in event:
        return send_uncategorized()
    return handle_webhook(event)


# ----------------------------------------------------------------------
# Sending uncategorized transactions
# ----------------------------------------------------------------------


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
        if send_expense(client, table, chat_id, item):
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
# Webhook (category, Change and Cancel button presses)
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
    message = update.get("message") or {}
    logger.info(
        "Telegram update received",
        extra={
            "request_id": request_id,
            "update_id": update.get("update_id"),
            "is_callback": callback_query is not None,
            "has_document": "document" in message,
        },
    )
    if callback_query:
        try:
            handle_callback(callback_query)
        except TelegramError:
            # Return 200 anyway so Telegram does not keep redelivering the update.
            logger.exception("Telegram API call failed", extra={"request_id": request_id})
    elif "document" in message:
        handle_document(message)
    return json_response(200, {"ok": True})


def handle_document(message: dict[str, Any]) -> bool:
    """Hand a CSV posted to the family chat to the Apple Card import; True if started.

    Other files are ignored without a reply: the chat is shared with people.
    """
    document = message["document"]
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (message.get("from") or {}).get("id")
    file_name = document.get("file_name") or ""
    file_size = document.get("file_size") or 0
    if str(chat_id) != os.environ.get("TELEGRAM_CHAT_ID"):
        logger.info("Document outside the family chat ignored", extra={"chat_id": chat_id})
        return False
    if not _is_allowed(user_id):
        logger.warning("Document from a user that is not allowed", extra={"user_id": user_id})
        return False
    if not file_name.lower().endswith(".csv"):
        logger.info("Document is not a CSV file; ignored")
        return False
    if file_size > MAX_CSV_BYTES:
        logger.warning("CSV file too large; ignored", extra={"file_size": file_size})
        return False

    boto3.client("lambda").invoke(
        FunctionName=os.environ["APPLE_CARD_IMPORT_FUNCTION"],
        InvocationType="Event",
        Payload=json.dumps(
            {
                "file_id": document["file_id"],
                "file_name": file_name,
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "user_id": user_id,
            }
        ).encode(),
    )
    logger.info("Apple Card import started", extra={"user_id": user_id, "file_size": file_size})
    return True


def handle_callback(callback_query: dict[str, Any]) -> None:
    client = _telegram_client()
    query_id = callback_query["id"]
    user_id = (callback_query.get("from") or {}).get("id")
    if not _is_allowed(user_id):
        logger.warning("Callback from a user that is not allowed", extra={"user_id": user_id})
        client.answer_callback_query(query_id, "Not allowed")
        return

    parts = (callback_query.get("data") or "").split(":")
    if len(parts) == 3 and parts[0] == CALLBACK_PREFIX and parts[2] in CATEGORIES:
        _handle_category(client, callback_query, parts[1], parts[2], user_id)
    elif len(parts) == 2 and parts[0] in (CHANGE_PREFIX, CANCEL_PREFIX) and parts[1]:
        _handle_change(client, callback_query, parts[1], changing=parts[0] == CHANGE_PREFIX)
    else:
        client.answer_callback_query(query_id, "Unknown category")


def _handle_category(
    client: TelegramClient,
    callback_query: dict[str, Any],
    transaction_id: str,
    category_key: str,
    user_id: int,
) -> None:
    query_id = callback_query["id"]
    item = _set_category(transaction_id, category_key, user_id)
    if item is None:
        client.answer_callback_query(query_id, "Transaction no longer exists")
        return

    logger.info(
        "Transaction categorized",
        extra={"transaction_id": item["transaction_id"], "expense_category": category_key},
    )
    client.answer_callback_query(query_id, f"Saved: {CATEGORIES[category_key]}")
    message = callback_query.get("message")
    if message:
        # Buttons keep the message's transaction ID; a pending one is followed to its posted row.
        client.edit_message_text(
            message["chat"]["id"],
            message["message_id"],
            format_categorized(item),
            reply_markup=categorized_keyboard(transaction_id),
        )


def _handle_change(
    client: TelegramClient, callback_query: dict[str, Any], transaction_id: str, changing: bool
) -> None:
    """Show the category buttons again (Change) or go back to the saved category (Cancel)."""
    query_id = callback_query["id"]
    item = _get_transaction(_table(), transaction_id)
    if item is None:
        client.answer_callback_query(query_id, "Transaction no longer exists")
        return

    client.answer_callback_query(query_id)
    message = callback_query.get("message")
    if not message:
        return
    current = item.get("expense_category")
    if changing or not current:
        text = format_categorized(item) if current else format_message(item)
        markup = category_keyboard(transaction_id, current=current)
    else:
        text = format_categorized(item)
        markup = categorized_keyboard(transaction_id)
    client.edit_message_text(message["chat"]["id"], message["message_id"], text, markup)


def _get_transaction(table, transaction_id: str) -> dict[str, Any] | None:
    item = table.get_item(Key={"transaction_id": transaction_id}).get("Item")
    if item is None:
        posted_id = _posted_transaction_id(table, transaction_id)
        if posted_id:
            item = table.get_item(Key={"transaction_id": posted_id}).get("Item")
    return item


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
