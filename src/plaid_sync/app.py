"""Plaid webhook handler - synchronizes transactions via Plaid Transactions Sync API.

Invoked by API Gateway (Plaid webhook) or directly with {"item_id": "..."}.
"""

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.aws_secrets import get_secret_json
from shared.http import json_response
from shared.plaid import PlaidClient, PlaidError

logger = logging.getLogger()

MUTATION_DURING_PAGINATION = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
MAX_PAGINATION_RESTARTS = 3

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


def lambda_handler(event, context):
    if "requestContext" not in event:
        return sync_item(event["item_id"])

    # Never log the request body: it may contain financial data.
    request_id = event["requestContext"].get("requestId")
    try:
        body = json.loads(event.get("body") or "")
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

    if webhook_type == "TRANSACTIONS" and webhook_code == "SYNC_UPDATES_AVAILABLE" and item_id:
        sync_item(item_id)
    return json_response(200, {"ok": True})


def sync_item(item_id: str) -> dict[str, Any]:
    secret, access_token = _plaid_secret(item_id)
    if not access_token:
        logger.warning("No access token for Plaid item", extra={"item_id": item_id})
        return {"item_id": item_id, "synced": False}

    items_table = _table("PLAID_ITEMS_TABLE")
    item = items_table.get_item(Key={"item_id": item_id}).get("Item") or {}
    start_cursor = item.get("cursor") or None
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
    page = _fetch_all_pages(client, access_token, start_cursor, days_requested)

    transactions_table = _table("TRANSACTIONS_TABLE")
    upserted = skipped = 0
    for txn in page["added"] + page["modified"]:
        if sync_start_date and txn["date"] < sync_start_date:
            skipped += 1
            continue
        _upsert_transaction(transactions_table, to_item(txn, item_id, page["accounts"]))
        upserted += 1

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
    _save_cursor(items_table, item_id, start_cursor, page["next_cursor"], counts)
    logger.info("Plaid item synced", extra={"item_id": item_id, **counts})
    return {"item_id": item_id, "synced": True, **counts}


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


def _upsert_transaction(table, item: dict[str, Any]) -> None:
    """Update only Plaid-owned fields, preserving user/bot fields like expense_category."""
    names = {"#updated_at": "updated_at", "#created_at": "created_at"}
    values: dict[str, Any] = {":now": _now()}
    sets = ["#updated_at = :now", "#created_at = if_not_exists(#created_at, :now)"]
    for i, (attr, value) in enumerate(item.items()):
        if attr == "transaction_id":
            continue
        names[f"#a{i}"] = attr
        values[f":v{i}"] = value
        sets.append(f"#a{i} = :v{i}")
    table.update_item(
        Key={"transaction_id": item["transaction_id"]},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _save_cursor(
    table, item_id: str, start_cursor: str | None, next_cursor: str, counts: dict[str, int]
) -> None:
    # Optimistic lock: only advance the cursor if no concurrent sync moved it first.
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
