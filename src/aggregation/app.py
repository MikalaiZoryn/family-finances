"""Monthly budget aggregation.

Invoked directly (manual now, a schedule later). Totals categorized expenses per
category for the month, updates CategoryStateTable and posts a report to the
Telegram chat. Event: {} for the current UTC month, or {"month": "YYYY-MM"}.

Budget rules (limit -1 = no limit; P = the previous month's state row):
- limit == -1                                   -> budget = -1
- limit >= 0 and (no P, or P had no limit)      -> budget = limit
- limit >= 0 and P had a limit                  -> budget = P.budget - P.expenses + limit
A negative remainder (overspending) carries over.
"""

import html
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from shared.aws_secrets import get_secret_json
from shared.categories import CATEGORIES
from shared.telegram import TelegramClient

logger = logging.getLogger()

NO_LIMIT = Decimal(-1)
CATEGORY_DATE_INDEX = "CategoryDateIndex"
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_dynamodb = None


def _resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _transactions_table():
    return _resource().Table(os.environ["TRANSACTIONS_TABLE"])


def _limits_table():
    return _resource().Table(os.environ["CATEGORY_LIMITS_TABLE"])


def _state_table():
    return _resource().Table(os.environ["CATEGORY_STATE_TABLE"])


def _telegram_client() -> TelegramClient:
    return TelegramClient(get_secret_json(os.environ["TELEGRAM_SECRET_ARN"])["bot_token"])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lambda_handler(event, context):
    month = (event or {}).get("month") or datetime.now(UTC).strftime("%Y-%m")
    rows = aggregate(month)
    _telegram_client().send_message(os.environ["TELEGRAM_CHAT_ID"], format_report(month, rows))
    logger.info("Budget aggregated", extra={"month": month})
    return {
        "month": month,
        "categories": [
            {
                "category": row["category"],
                "limit": str(row["limit"]),
                "budget": str(row["budget"]),
                "current_expenses": str(row["current_expenses"]),
            }
            for row in rows
        ],
    }


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


def previous_month(month: str) -> str:
    if not MONTH_PATTERN.match(month):
        raise ValueError(f"Invalid month {month!r}, expected YYYY-MM")
    year, mon = (int(part) for part in month.split("-"))
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def has_limit(limit: Decimal) -> bool:
    return limit >= 0


def compute_budget(limit: Decimal, prev: dict[str, Any] | None) -> Decimal:
    """Budget for a month from its limit and the previous month's state row."""
    if not has_limit(limit):
        return NO_LIMIT
    # Decided by the previous limit, not budget: a carried budget can be negative.
    if prev is None or not has_limit(prev["limit"]):
        return limit
    return prev["budget"] - prev["current_expenses"] + limit


def month_expenses(category: str, month: str) -> Decimal:
    """Sum of categorized transaction amounts dated in the month."""
    table = _transactions_table()
    kwargs: dict[str, Any] = {
        "IndexName": CATEGORY_DATE_INDEX,
        "KeyConditionExpression": Key("expense_category").eq(category)
        & Key("transaction_date").between(f"{month}-01", f"{month}-31"),
        "ProjectionExpression": "amount",
    }
    total = Decimal(0)
    while True:
        response = table.query(**kwargs)
        total += sum((Decimal(str(item["amount"])) for item in response["Items"]), Decimal(0))
        if "LastEvaluatedKey" not in response:
            return total
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _load_limits() -> dict[str, Decimal]:
    table = _limits_table()
    kwargs: dict[str, Any] = {}
    limits: dict[str, Decimal] = {}
    while True:
        response = table.scan(**kwargs)
        for item in response["Items"]:
            limits[item["category"]] = Decimal(str(item["limit"]))
        if "LastEvaluatedKey" not in response:
            return limits
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def aggregate(month: str) -> list[dict[str, Any]]:
    """Update the month's state rows (and the previous month's expenses); return the rows."""
    prev_month = previous_month(month)
    limits = _load_limits()
    state = _state_table()
    now = _now()
    rows = []
    for category in CATEGORIES:
        prev = state.get_item(Key={"category": category, "month": prev_month}).get("Item")
        if prev is not None:
            # Late categorizations of last month's transactions change its remainder.
            prev["current_expenses"] = month_expenses(category, prev_month)
            prev["updated_at"] = now
            state.put_item(Item=prev)

        current = state.get_item(Key={"category": category, "month": month}).get("Item")
        # The limit is fixed when the month's row is created; later changes apply next month.
        limit = current["limit"] if current else limits.get(category, NO_LIMIT)
        row = {
            "category": category,
            "month": month,
            "limit": limit,
            "budget": compute_budget(limit, prev),
            "current_expenses": month_expenses(category, month),
            "updated_at": now,
        }
        state.put_item(Item=row)
        rows.append(row)
    return rows


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def format_report(month: str, rows: list[dict[str, Any]]) -> str:
    title = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    lines = [f"📊 <b>Budget · {html.escape(title)}</b>", ""]
    total = Decimal(0)
    for row in rows:
        label = html.escape(CATEGORIES.get(row["category"], row["category"]))
        spent = row["current_expenses"]
        total += spent
        if not has_limit(row["limit"]):
            lines.append(f"{label}: {_money(spent)} · no limit")
            continue
        remaining = row["budget"] - spent
        status = f"{_money(remaining)} left" if remaining >= 0 else f"⚠️ {_money(-remaining)} over"
        lines.append(f"{label}: {_money(spent)} / {_money(row['budget'])} · {status}")
    lines += ["", f"<b>Total spent: {_money(total)}</b>"]
    return "\n".join(lines)
