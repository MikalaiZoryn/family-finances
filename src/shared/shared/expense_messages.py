"""Telegram messages that ask the user to categorize an expense.

Used by the Plaid sync (new transactions, right after a sync) and by the Telegram bot
(sweep of anything not sent yet, and button presses).
"""

import html
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shared.categories import CATEGORIES
from shared.telegram import TelegramClient

logger = logging.getLogger()

BUTTONS_PER_ROW = 2
CALLBACK_PREFIX = "cat"
MAX_CALLBACK_DATA_BYTES = 64

# Not expenses the user needs to categorize.
EXCLUDED_PRIMARY_CATEGORIES = {"TRANSFER_IN", "TRANSFER_OUT", "INCOME"}
EXCLUDED_DETAILED_CATEGORIES = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}


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
