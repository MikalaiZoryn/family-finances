"""Plaid webhook handler - synchronizes transactions via Plaid Transactions Sync API.

Invoked by API Gateway (Plaid webhook) or directly with {"item_id": "..."}. Webhooks are
verified, acknowledged at once, and the sync runs in an async invoke of this function.
"""

import base64
import html
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared import plaid_webhook
from shared.aws_secrets import get_secret_json
from shared.expense_messages import is_categorizable, send_expense
from shared.http import json_response
from shared.plaid import PlaidClient, PlaidError
from shared.telegram import TelegramClient, TelegramError

logger = logging.getLogger()

MUTATION_DURING_PAGINATION = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
# Plaid only sends SYNC_UPDATES_AVAILABLE after /transactions/sync has been called once,
# so the legacy update webhooks must also trigger a sync to bootstrap new Items.
SYNC_WEBHOOK_CODES = {
    "SYNC_UPDATES_AVAILABLE",
    "INITIAL_UPDATE",
    "HISTORICAL_UPDATE",
    "DEFAULT_UPDATE",
}
MAX_PAGINATION_RESTARTS = 3
# Sync errors that only the user can fix by reconnecting (Link update mode); retrying won't help.
NEEDS_UPDATE_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "INVALID_CREDENTIALS",
    "INVALID_MFA",
    "ITEM_LOCKED",
    "ACCESS_NOT_GRANTED",
    "INSUFFICIENT_CREDENTIALS",
    "USER_SETUP_REQUIRED",
    "PASSWORD_RESET_REQUIRED",
}
# The Item is gone for good: it must be removed and linked again.
REVOKED_CODES = {"ITEM_NOT_FOUND", "USER_PERMISSION_REVOKED", "USER_ACCOUNT_REVOKED"}

# PlaidItemsTable `status` values. Absent means ok.
STATUS_OK = "ok"
STATUS_NEEDS_UPDATE = "needs_update"
STATUS_PENDING_DISCONNECT = "pending_disconnect"
STATUS_NEW_ACCOUNTS = "new_accounts"
STATUS_REVOKED = "revoked"
LINK_SCRIPT = "python scripts/plaid_link.py"
# User/bot fields copied from a pending transaction to its posted replacement.
CARRY_OVER_ATTRIBUTES = (
    "expense_category",
    "categorized_at",
    "categorized_by",
    "telegram_message_id",
    "telegram_chat_id",
    "telegram_sent_at",
)

_dynamodb = None


def _table(env_var: str):
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(os.environ[env_var])


def _plaid_secret(item_id: str) -> tuple[dict[str, Any], str | None]:
    secret_arn = os.environ["PLAID_SECRET_ARN"]
    secret = get_secret_json(secret_arn)
    if item_id not in secret.get("access_tokens", {}):
        # The token may have been added after this container cached the secret.
        secret = get_secret_json(secret_arn, refresh=True)
    return secret, secret.get("access_tokens", {}).get(item_id)


def _plaid_client(secret: dict[str, Any]) -> PlaidClient:
    return PlaidClient(secret["client_id"], secret["secret"], secret.get("env", "sandbox"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _start_sync(item_id: str) -> None:
    """Run the sync in an async invoke of this function so the webhook is acknowledged at once."""
    boto3.client("lambda").invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"item_id": item_id}).encode(),
    )


def _telegram_client() -> TelegramClient:
    return TelegramClient(get_secret_json(os.environ["TELEGRAM_SECRET_ARN"])["bot_token"])


def _telegram_alert(text: str) -> None:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set; Plaid item alert not sent")
        return
    try:
        _telegram_client().send_message(chat_id, text)
    except (TelegramError, KeyError) as e:
        logger.error("Plaid item alert not sent", extra={"error": str(e)})


def _raw_body(event) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()


def lambda_handler(event, context):
    if "requestContext" not in event:
        return sync_item(event["item_id"])

    # Never log the request body: it may contain financial data.
    request_id = event["requestContext"].get("requestId")
    raw_body = _raw_body(event)
    secret = get_secret_json(os.environ["PLAID_SECRET_ARN"])
    if not plaid_webhook.verify(_plaid_client(secret), event.get("headers") or {}, raw_body):
        logger.warning("Plaid webhook verification failed", extra={"request_id": request_id})
        return json_response(401, {"ok": False})
    try:
        body = json.loads(raw_body)
    except ValueError:
        logger.warning("Invalid Plaid webhook body", extra={"request_id": request_id})
        return json_response(400, {"ok": False})

    webhook_type = body.get("webhook_type")
    webhook_code = body.get("webhook_code")
    item_id = body.get("item_id")
    logger.info(
        "Plaid webhook received",
        extra={
            "request_id": request_id,
            "webhook_type": webhook_type,
            "webhook_code": webhook_code,
            "item_id": item_id,
        },
    )

    if webhook_type == "TRANSACTIONS" and webhook_code in SYNC_WEBHOOK_CODES and item_id:
        _start_sync(item_id)
    elif webhook_type == "ITEM" and item_id:
        handle_item_webhook(webhook_code, item_id, body.get("error") or {})
    return json_response(200, {"ok": True})


def handle_item_webhook(webhook_code: str, item_id: str, error: dict[str, Any]) -> None:
    """Track Item health and tell the family in Telegram when a bank needs attention."""
    if webhook_code == "ERROR":
        code = error.get("error_code")
        status = STATUS_REVOKED if code in REVOKED_CODES else STATUS_NEEDS_UPDATE
        set_item_status(item_id, status, code)
    elif webhook_code in ("PENDING_DISCONNECT", "PENDING_EXPIRATION"):
        set_item_status(item_id, STATUS_PENDING_DISCONNECT, webhook_code)
    elif webhook_code == "USER_PERMISSION_REVOKED":
        set_item_status(item_id, STATUS_REVOKED, webhook_code)
    elif webhook_code == "LOGIN_REPAIRED":
        set_item_status(item_id, STATUS_OK, webhook_code)
    elif webhook_code == "NEW_ACCOUNTS_AVAILABLE":
        set_item_status(item_id, STATUS_NEW_ACCOUNTS, webhook_code, only_if_ok=True)


def set_item_status(item_id: str, status: str, reason: str | None, only_if_ok=False) -> bool:
    """Set the Item's status and send one Telegram alert per change. Returns True if changed."""
    condition = "attribute_exists(item_id) AND (attribute_not_exists(#status) OR #status <> :s)"
    values = {":s": status, ":r": reason or "", ":now": _now()}
    if only_if_ok:
        condition += " AND (attribute_not_exists(#status) OR #status = :ok)"
        values[":ok"] = STATUS_OK
    try:
        row = _table("PLAID_ITEMS_TABLE").update_item(
            Key={"item_id": item_id},
            UpdateExpression="SET #status = :s, status_reason = :r, status_updated_at = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False  # Unknown Item, or the status is unchanged (webhooks are redelivered).

    logger.info("Plaid item status changed", extra={"item_id": item_id, "status": status})
    _telegram_alert(alert_text(row))
    return True


def alert_text(row: dict[str, Any]) -> str:
    item_id = row["item_id"]
    bank = html.escape(row.get("institution_name") or item_id)
    reason = html.escape(row.get("status_reason") or "")
    update = f"<code>{LINK_SCRIPT} update {item_id}</code>"
    match row["status"]:
        case "needs_update":
            return (
                f"⚠️ <b>{bank}</b> needs to be reconnected ({reason}). "
                f"Transactions are not syncing until you run:\n{update}"
            )
        case "pending_disconnect":
            return f"⏳ Access to <b>{bank}</b> expires within 7 days. Renew it with:\n{update}"
        case "new_accounts":
            return f"🆕 <b>{bank}</b> has new accounts. To share them, run:\n{update}"
        case "revoked":
            return (
                f"⛔ Access to <b>{bank}</b> was revoked ({reason}). To connect it again, run:\n"
                f"<code>{LINK_SCRIPT} remove {item_id}</code>\n<code>{LINK_SCRIPT} link</code>"
            )
        case _:
            return f"✅ <b>{bank}</b> is connected again."


def sync_item(item_id: str) -> dict[str, Any]:
    secret, access_token = _plaid_secret(item_id)
    if not access_token:
        logger.warning("No access token for Plaid item", extra={"item_id": item_id})
        return {"item_id": item_id, "synced": False}

    items_table = _table("PLAID_ITEMS_TABLE")
    item = items_table.get_item(Key={"item_id": item_id}).get("Item") or {}
    start_cursor = item.get("cursor") or None
    previous_status = item.get("status")
    sync_start_date = item.get("sync_start_date")

    # Initial sync: only fetch and keep the last INITIAL_SYNC_DAYS of history.
    days_requested = None
    if not start_cursor:
        days_requested = int(os.environ.get("INITIAL_SYNC_DAYS", "30"))
        if not sync_start_date:
            sync_start_date = (date.today() - timedelta(days=days_requested)).isoformat()
            items_table.update_item(
                Key={"item_id": item_id},
                UpdateExpression=(
                    "SET sync_start_date = :d, created_at = if_not_exists(created_at, :now)"
                ),
                ExpressionAttributeValues={":d": sync_start_date, ":now": _now()},
            )

    client = _plaid_client(secret)
    try:
        page = _fetch_all_pages(client, access_token, start_cursor, days_requested)
    except PlaidError as e:
        logger.warning(
            "Plaid sync failed",
            extra={"item_id": item_id, "error_code": e.error_code, "request_id": e.request_id},
        )
        if e.error_code in NEEDS_UPDATE_CODES:
            set_item_status(item_id, STATUS_NEEDS_UPDATE, e.error_code)
        elif e.error_code in REVOKED_CODES:
            set_item_status(item_id, STATUS_REVOKED, e.error_code)
        else:
            raise  # Transient (e.g. INSTITUTION_DOWN): the async invoke retries with backoff.
        return {"item_id": item_id, "synced": False, "error_code": e.error_code}

    transactions_table = _table("TRANSACTIONS_TABLE")
    upserted = skipped = 0
    # Added and removed again within this sync (the rows are deleted below): nothing to send.
    removed_ids = {removed["transaction_id"] for removed in page["removed"]}
    added_ids = {txn["transaction_id"] for txn in page["added"]} - removed_ids
    added_rows = []
    for txn in page["added"] + page["modified"]:
        if sync_start_date and txn["date"] < sync_start_date:
            skipped += 1
            continue
        item = to_item(txn, item_id, page["accounts"])
        carry_over = _pending_carry_over(transactions_table, item.get("pending_transaction_id"))
        row = _upsert_transaction(transactions_table, item, carry_over)
        if txn["transaction_id"] in added_ids:
            added_rows.append(row)
        upserted += 1

    # Deleted after the upserts so posted transactions can inherit from their pending rows.
    # Dedupe keys: BatchWriteItem rejects duplicate keys in one request.
    with transactions_table.batch_writer(overwrite_by_pkeys=["transaction_id"]) as batch:
        for removed in page["removed"]:
            batch.delete_item(Key={"transaction_id": removed["transaction_id"]})

    counts = {
        "added": len(page["added"]),
        "modified": len(page["modified"]),
        "removed": len(page["removed"]),
        "upserted": upserted,
        "skipped_before_start_date": skipped,
    }
    if _save_cursor(items_table, item_id, start_cursor, page["next_cursor"], counts):
        # Only the run that advanced the cursor notifies, so a duplicate webhook that
        # synced the same page concurrently does not send the same expenses twice.
        counts["telegram_sent"] = notify_new_expenses(transactions_table, added_rows)
    if previous_status == STATUS_NEEDS_UPDATE:
        # The login works again (e.g. reconnected without a LOGIN_REPAIRED webhook yet).
        set_item_status(item_id, STATUS_OK, "SYNC_SUCCEEDED")
    logger.info("Plaid item synced", extra={"item_id": item_id, **counts})
    return {"item_id": item_id, "synced": True, **counts}


def notify_new_expenses(table, rows: list[dict[str, Any]]) -> int:
    """Send just-added expenses to Telegram for categorization; return how many were sent.

    Never raises: the cursor is already saved, so whatever is not sent here is left for
    the Telegram bot's sweep of unsent expenses.
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
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set; new expenses not sent")
        return 0
    max_messages = int(os.environ.get("MAX_MESSAGES_PER_SYNC", "20"))
    if len(to_send) > max_messages:
        logger.info(
            "More new expenses than MAX_MESSAGES_PER_SYNC; the rest wait for the sweep",
            extra={"new_expenses": len(to_send), "max_messages": max_messages},
        )
    sent = 0
    try:
        client = _telegram_client()
        for row in to_send[:max_messages]:
            if send_expense(client, table, chat_id, row):
                sent += 1
    except (TelegramError, KeyError) as e:
        logger.error("New expenses not sent to Telegram", extra={"error": str(e), "sent": sent})
    return sent


def _fetch_all_pages(
    client: PlaidClient, access_token: str, start_cursor: str | None, days_requested: int | None
) -> dict[str, Any]:
    for _ in range(MAX_PAGINATION_RESTARTS):
        added, modified, removed, accounts = [], [], [], {}
        cursor = start_cursor
        try:
            while True:
                response = client.transactions_sync(
                    access_token, cursor, days_requested=days_requested
                )
                added.extend(response.get("added", []))
                modified.extend(response.get("modified", []))
                removed.extend(response.get("removed", []))
                for account in response.get("accounts", []):
                    accounts[account["account_id"]] = account
                cursor = response["next_cursor"]
                if not response.get("has_more"):
                    break
        except PlaidError as e:
            if e.error_code == MUTATION_DURING_PAGINATION:
                logger.info("Plaid data changed during pagination, restarting sync")
                continue
            raise
        return {
            "added": added,
            "modified": modified,
            "removed": removed,
            "accounts": accounts,
            "next_cursor": cursor,
        }
    raise RuntimeError("Plaid sync kept changing during pagination")


def to_item(txn: dict[str, Any], item_id: str, accounts: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Plaid transaction into Plaid-owned TransactionsTable attributes."""
    account = accounts.get(txn["account_id"], {})
    category = txn.get("personal_finance_category") or {}
    item = {
        "transaction_id": txn["transaction_id"],
        "item_id": item_id,
        "account_id": txn["account_id"],
        "account_name": account.get("name"),
        "account_mask": account.get("mask"),
        "amount": Decimal(str(txn["amount"])),
        "currency": txn.get("iso_currency_code") or txn.get("unofficial_currency_code"),
        "transaction_date": txn["date"],
        "authorized_date": txn.get("authorized_date"),
        "name": txn.get("name"),
        "merchant_name": txn.get("merchant_name"),
        "status": "pending" if txn.get("pending") else "posted",
        "pending_transaction_id": txn.get("pending_transaction_id"),
        "plaid_category_primary": category.get("primary"),
        "plaid_category_detailed": category.get("detailed"),
        "payment_channel": txn.get("payment_channel"),
    }
    # Omit nulls rather than storing them as NULL.
    return {k: v for k, v in item.items() if v is not None}


def _pending_carry_over(table, pending_transaction_id: str | None) -> dict[str, Any]:
    """User/bot fields of the pending transaction that this posted transaction replaces."""
    if not pending_transaction_id:
        return {}
    pending = table.get_item(Key={"transaction_id": pending_transaction_id}).get("Item") or {}
    return {k: pending[k] for k in CARRY_OVER_ATTRIBUTES if k in pending}


def _upsert_transaction(
    table, item: dict[str, Any], carry_over: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Update only Plaid-owned fields, preserving user/bot fields like expense_category.

    carry_over attributes are set only if the item does not already have them.
    Returns the stored row.
    """
    names = {"#updated_at": "updated_at", "#created_at": "created_at"}
    values: dict[str, Any] = {":now": _now()}
    sets = ["#updated_at = :now", "#created_at = if_not_exists(#created_at, :now)"]
    for i, (attr, value) in enumerate(item.items()):
        if attr == "transaction_id":
            continue
        names[f"#a{i}"] = attr
        values[f":v{i}"] = value
        sets.append(f"#a{i} = :v{i}")
    for i, (attr, value) in enumerate((carry_over or {}).items()):
        names[f"#c{i}"] = attr
        values[f":c{i}"] = value
        sets.append(f"#c{i} = if_not_exists(#c{i}, :c{i})")
    return table.update_item(
        Key={"transaction_id": item["transaction_id"]},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )["Attributes"]


def _save_cursor(
    table, item_id: str, start_cursor: str | None, next_cursor: str, counts: dict[str, int]
) -> bool:
    """Advance the cursor; False if a concurrent sync moved it first (optimistic lock)."""
    values = {":new": next_cursor, ":old": start_cursor or "", ":now": _now(), ":counts": counts}
    try:
        table.update_item(
            Key={"item_id": item_id},
            UpdateExpression=(
                "SET #cursor = :new, last_synced_at = :now, last_sync_counts = :counts"
            ),
            ConditionExpression="attribute_not_exists(#cursor) OR #cursor = :old",
            ExpressionAttributeNames={"#cursor": "cursor"},
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        logger.warning("Cursor changed by a concurrent sync; not saved", extra={"item_id": item_id})
        return False
    return True
