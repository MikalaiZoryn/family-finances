"""Apple Card CSV import.

Invoked asynchronously by the Telegram bot when an allowed user posts a CSV to the
family chat. Event: {"file_id", "file_name", "chat_id", "message_id", "user_id"}.
Downloads the file, stores its transactions in TransactionsTable, replies with a summary
and sends the new expenses for categorization.

Apple Card exports have no transaction ID, and the same transactions show up again in
every later export, so file rows are matched to stored rows (see reconcile).
"""

import csv
import hashlib
import html
import io
import logging
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

from shared.aws_secrets import get_secret_json
from shared.expense_messages import send_new_expenses
from shared.telegram import TelegramClient
from shared.transactions import upsert_transaction

logger = logging.getLogger()

SOURCE = "apple_card"
ACCOUNT_NAME = "Apple Card"
MAX_CSV_BYTES = 1_000_000
COLUMNS = {
    "transaction_date": "Transaction Date",
    "clearing_date": "Clearing Date",
    "name": "Description",
    "merchant_name": "Merchant",
    "apple_category": "Category",
    "apple_type": "Type",
    "amount": "Amount (USD)",
    "purchased_by": "Purchased By",
}
# Attributes owned by the CSV: a re-import updates them when they change.
CSV_OWNED_ATTRIBUTES = (
    "transaction_date",
    "clearing_date",
    "status",
    "name",
    "merchant_name",
    "apple_category",
    "apple_type",
    "amount",
    "currency",
    "purchased_by",
    "account_name",
    "source",
)

_dynamodb = None


class CsvFormatError(ValueError):
    """The file is not an Apple Card export."""


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(os.environ["TRANSACTIONS_TABLE"])


def _telegram_client() -> TelegramClient:
    return TelegramClient(get_secret_json(os.environ["TELEGRAM_SECRET_ARN"])["bot_token"])


def lambda_handler(event, context):
    client = _telegram_client()
    chat_id = event["chat_id"]
    message_id = event.get("message_id")
    try:
        file = client.get_file(event["file_id"])
        content = client.download_file(file["file_path"], MAX_CSV_BYTES)
        result = import_rows(parse_csv(content), event.get("file_name"), event.get("user_id"))
    except CsvFormatError as e:
        logger.info("File is not an Apple Card export", extra={"error": str(e)})
        client.send_message(
            chat_id,
            f"🍏 This doesn't look like an Apple Card export: {html.escape(str(e))}",
            reply_to_message_id=message_id,
        )
        return {"imported": False, "error": str(e)}
    except Exception:
        logger.exception("Apple Card import failed")
        client.send_message(
            chat_id,
            "🍏 Apple Card import failed. Please try again later.",
            reply_to_message_id=message_id,
        )
        raise

    counts = result["counts"]
    counts["telegram_sent"] = send_new_expenses(
        lambda: client,
        _table(),
        os.environ.get("TELEGRAM_CHAT_ID"),
        result["new_rows"],
        int(os.environ.get("MAX_MESSAGES_PER_IMPORT", "20")),
    )
    client.send_message(chat_id, summary_text(counts), reply_to_message_id=message_id)
    logger.info("Apple Card import done", extra=counts)
    return {"imported": True, **counts}


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def _iso_date(value: str, column: str, line: int) -> str:
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError:
        raise CsvFormatError(f"line {line}: bad {column} {value!r}") from None


def parse_csv(content: bytes) -> list[dict[str, Any]]:
    """Parse an export into TransactionsTable attributes (without transaction_id)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvFormatError("not a UTF-8 text file") from None
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in COLUMNS.values() if c not in (reader.fieldnames or [])]
    if missing:
        raise CsvFormatError(f"missing columns {', '.join(missing)}")

    rows = []
    for record in reader:
        line = reader.line_num
        raw = {attr: (record.get(column) or "").strip() for attr, column in COLUMNS.items()}
        if not any(raw.values()):
            continue
        try:
            amount = Decimal(raw["amount"])
        except InvalidOperation:
            raise CsvFormatError(f"line {line}: bad amount {raw['amount']!r}") from None
        row = {
            **raw,
            "transaction_date": _iso_date(raw["transaction_date"], "Transaction Date", line),
            "clearing_date": (
                _iso_date(raw["clearing_date"], "Clearing Date", line)
                if raw["clearing_date"]
                else ""
            ),
            "status": "posted" if raw["clearing_date"] else "pending",
            "amount": amount,
            "currency": "USD",
            "account_name": ACCOUNT_NAME,
            "source": SOURCE,
        }
        # Omit empty values rather than storing empty strings.
        rows.append({k: v for k, v in row.items() if v != ""})
    if not rows:
        raise CsvFormatError("no transactions in the file")
    return rows


# ----------------------------------------------------------------------
# Matching against stored rows
# ----------------------------------------------------------------------


def _exact_key(row: dict[str, Any]) -> tuple:
    return (
        row["transaction_date"],
        row.get("name", ""),
        Decimal(str(row["amount"])),
        row.get("purchased_by", ""),
    )


def _loose_key(row: dict[str, Any]) -> tuple:
    return row["transaction_date"], row.get("name", ""), row.get("purchased_by", "")


def _transaction_id(row: dict[str, Any], n: int) -> str:
    date, name, amount, purchased_by = _exact_key(row)
    digest = hashlib.sha256(f"{date}|{name}|{amount}|{purchased_by}|{n}".encode()).hexdigest()
    return f"apple-{digest[:24]}"


def _stored_rows(table, start: str, end: str) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("source").eq(SOURCE)
        & Attr("transaction_date").between(start, end)
    }
    items: list[dict[str, Any]] = []
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def reconcile(
    rows: list[dict[str, Any]], stored: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair each file row with the stored row it is (or None if new), one-to-one.

    1. Exact: same date, description, amount and buyer.
    2. Loose: same date, description and buyer, different amount (e.g. a tip added when
       the charge cleared). The stored row is updated in place and keeps its category.
    Stored rows missing from the file are left alone.
    """
    exact_pool: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(stored, key=lambda item: item["transaction_id"]):
        exact_pool[_exact_key(item)].append(item)

    matches: list[dict[str, Any] | None] = []
    for row in rows:
        pool = exact_pool.get(_exact_key(row))
        matches.append(pool.pop(0) if pool else None)

    loose_pool: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for pool in exact_pool.values():
        for item in pool:
            loose_pool[_loose_key(item)].append(item)
    for i, row in enumerate(rows):
        if matches[i] is None:
            pool = loose_pool.get(_loose_key(row))
            matches[i] = pool.pop(0) if pool else None
    return list(zip(rows, matches, strict=True))


def _changed(row: dict[str, Any], stored: dict[str, Any]) -> bool:
    # Only SETs are written, so a value that went empty in the file is not compared.
    return any(
        attr in row and row[attr] != stored.get(attr) for attr in CSV_OWNED_ATTRIBUTES
    )


def import_rows(
    rows: list[dict[str, Any]], file_name: str | None, user_id: int | None
) -> dict[str, Any]:
    table = _table()
    dates = [row["transaction_date"] for row in rows]
    stored = _stored_rows(table, min(dates), max(dates))
    taken_ids = {item["transaction_id"] for item in stored}

    counts = {"rows": len(rows), "new": 0, "updated": 0, "unchanged": 0}
    new_rows = []
    for row, match in reconcile(rows, stored):
        if match is not None:
            if not _changed(row, match):
                counts["unchanged"] += 1
                continue
            upsert_transaction(table, {"transaction_id": match["transaction_id"], **row})
            counts["updated"] += 1
            continue

        # Identical rows in one file (two coffees on the same day) get different IDs; an ID
        # can also be held by a stored row whose amount changed since it was first seen.
        n = 0
        while (transaction_id := _transaction_id(row, n)) in taken_ids:
            n += 1
        taken_ids.add(transaction_id)
        item = {"transaction_id": transaction_id, **row}
        if file_name:
            item["import_file_name"] = file_name
        if user_id is not None:
            item["imported_by"] = user_id
        new_rows.append(upsert_transaction(table, item))
        counts["new"] += 1
    return {"counts": counts, "new_rows": new_rows}


def summary_text(counts: dict[str, int]) -> str:
    text = (
        f"🍏 Apple Card import: {counts['new']} new, {counts['updated']} updated, "
        f"{counts['unchanged']} unchanged"
    )
    if counts.get("telegram_sent"):
        text += f" · {counts['telegram_sent']} sent for categorization"
    return text
