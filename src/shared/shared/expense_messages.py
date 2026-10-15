"""Telegram messages that ask the user to categorize an expense.

Used by the Plaid sync and the Apple Card import (new transactions, right after they are
stored) and by the Telegram bot (sweep of anything not sent yet, and button presses).
"""

import html
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shared.categories import CATEGORIES
from shared.telegram import TelegramClient, TelegramError

logger = logging.getLogger()

BUTTONS_PER_ROW = 3
CALLBACK_PREFIX = "cat"
CHANGE_PREFIX = "chg"
CANCEL_PREFIX = "cnl"
MAX_CALLBACK_DATA_BYTES = 64

# Not expenses the user needs to categorize.
EXCLUDED_PRIMARY_CATEGORIES = {"TRANSFER_IN", "TRANSFER_OUT", "INCOME"}
EXCLUDED_DETAILED_CATEGORIES = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}
EXCLUDED_APPLE_TYPES = {"Payment"}


def is_categorizable(item: dict[str, Any]) -> bool:
    """True for expenses the user should categorize (not transfers, income, card payments)."""
    if Decimal(str(item.get("amount", 0))) <= 0:
        return False
    if item.get("plaid_category_primary") in EXCLUDED_PRIMARY_CATEGORIES:
        return False
    if item.get("apple_type") in EXCLUDED_APPLE_TYPES:
        return False
    return item.get("plaid_category_detailed") not in EXCLUDED_DETAILED_CATEGORIES


def callback_data(transaction_id: str, category_key: str) -> str:
    return f"{CALLBACK_PREFIX}:{transaction_id}:{category_key}"


def category_keyboard(transaction_id: str, current: str | None = None) -> dict[str, Any]:
    """Category buttons; when changing a category, the current one is ticked and Cancel added."""
    buttons = [
        {
            "text": f"{label} ✓" if key == current else label,
            "callback_data": callback_data(transaction_id, key),
        }
        for key, label in CATEGORIES.items()
    ]
    rows = [buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    if current:
        rows.append(
            [{"text": "↩️ Cancel", "callback_data": f"{CANCEL_PREFIX}:{transaction_id}"}]
        )
    return {"inline_keyboard": rows}


def categorized_keyboard(transaction_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "✏️ Change", "callback_data": f"{CHANGE_PREFIX}:{transaction_id}"}]
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
    if item.get("purchased_by"):
        lines.append(f"By {html.escape(item['purchased_by'])}")
    plaid_category = item.get("plaid_category_detailed") or item.get("plaid_category_primary")
    if plaid_category:
        lines.append(f"Plaid: {html.escape(plaid_category)}")
    if item.get("apple_category"):
        lines.append(f"Apple: {html.escape(item['apple_category'])}")
    return "\n".join(lines)


def format_categorized(item: dict[str, Any]) -> str:
    label = CATEGORIES.get(item["expense_category"], item["expense_category"])
    return f"{format_message(item)}\n\n✅ Category: <b>{html.escape(label)}</b>"


def send_expense(client: TelegramClient, table, chat_id: int | str, item: dict[str, Any]) -> bool:
    """Send the categorization message and record it on the row; False if it was skipped."""
    transaction_id = item["transaction_id"]
    if any(
        len(callback_data(transaction_id, key).encode()) > MAX_CALLBACK_DATA_BYTES
        for key in CATEGORIES
    ):
        logger.warning(
            "Transaction ID too long for callback data", extra={"transaction_id": transaction_id}
        )
        return False
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
            ":now": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    return True


def send_new_expenses(
    client_factory: Callable[[], TelegramClient],
    table,
    chat_id: int | str | None,
    rows: list[dict[str, Any]],
    max_messages: int,
) -> int:
    """Send just-stored expenses for categorization; return how many were sent.

    Skips rows already sent or categorized (e.g. inherited from a pending row). Oldest
    first, at most max_messages. Never raises: whatever is not sent here is left for the
    Telegram bot's sweep of unsent expenses.
    """
    to_send = sorted(
        (
            row
            for row in rows
            if is_categorizable(row)
            and "telegram_message_id" not in row
            and "expense_category" not in row
        ),
        key=lambda row: (row["transaction_date"], row["transaction_id"]),
    )
    if not to_send:
        return 0
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set; new expenses not sent")
        return 0
    if len(to_send) > max_messages:
        logger.info(
            "More new expenses than the per-run limit; the rest wait for the sweep",
            extra={"new_expenses": len(to_send), "max_messages": max_messages},
        )
    sent = 0
    try:
        client = client_factory()
        for row in to_send[:max_messages]:
            if send_expense(client, table, chat_id, row):
                sent += 1
    except (TelegramError, KeyError) as e:
        logger.error("New expenses not sent to Telegram", extra={"error": str(e), "sent": sent})
    return sent
