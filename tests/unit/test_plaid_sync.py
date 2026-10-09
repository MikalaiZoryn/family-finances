import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from plaid_sync import app
from shared import aws_secrets
from shared.plaid import PlaidError

EVENTS = Path(__file__).parents[2] / "events"
ITEM_ID = "sandbox-item-id"
RECENT = (date.today() - timedelta(days=2)).isoformat()
OLD = (date.today() - timedelta(days=60)).isoformat()


def txn(transaction_id, txn_date=RECENT, pending=False, amount=12.34, **extra):
    return {
        "transaction_id": transaction_id,
        "account_id": "acc-1",
        "amount": amount,
        "iso_currency_code": "USD",
        "date": txn_date,
        "authorized_date": None,
        "name": "Coffee Shop",
        "merchant_name": "Coffee Shop",
        "pending": pending,
        "pending_transaction_id": None,
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_COFFEE",
        },
        "payment_channel": "in store",
        **extra,
    }


def page(added=(), modified=(), removed=(), next_cursor="cursor-1", has_more=False):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": [{"transaction_id": t} for t in removed],
        "accounts": [{"account_id": "acc-1", "name": "Checking", "mask": "0000"}],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


class FakePlaid:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def transactions_sync(self, access_token, cursor=None, count=500, days_requested=None):
        self.calls.append({"cursor": cursor, "days_requested": days_requested})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        transactions = dynamodb.create_table(
            TableName="transactions",
            KeySchema=[{"AttributeName": "transaction_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "transaction_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        items = dynamodb.create_table(
            TableName="plaid-items",
            KeySchema=[{"AttributeName": "item_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "item_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        secret_arn = boto3.client("secretsmanager").create_secret(
            Name="plaid",
            SecretString=json.dumps(
                {
                    "client_id": "id",
                    "secret": "s",
                    "env": "sandbox",
                    "access_tokens": {ITEM_ID: "access-sandbox-token"},
                }
            ),
        )["ARN"]
        monkeypatch.setenv("TRANSACTIONS_TABLE", "transactions")
        monkeypatch.setenv("PLAID_ITEMS_TABLE", "plaid-items")
        monkeypatch.setenv("PLAID_SECRET_ARN", secret_arn)
        monkeypatch.setenv("INITIAL_SYNC_DAYS", "30")
        monkeypatch.setattr(app, "_dynamodb", None)
        aws_secrets.clear_cache()
        yield {"transactions": transactions, "items": items}
        aws_secrets.clear_cache()


@pytest.fixture
def plaid(monkeypatch):
    fake = FakePlaid([])
    monkeypatch.setattr(app, "_plaid_client", lambda secret: fake)
    return fake


def webhook_event(body):
    event = json.loads((EVENTS / "plaid_webhook.json").read_text())
    event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def get_txn(aws, transaction_id):
    return aws["transactions"].get_item(Key={"transaction_id": transaction_id}).get("Item")


def get_plaid_item(aws):
    return aws["items"].get_item(Key={"item_id": ITEM_ID}).get("Item")


def test_webhook_syncs_added_transactions(aws, plaid):
    plaid.responses = [page(added=[txn("t1"), txn("t2", pending=True)])]

    response = app.lambda_handler(json.loads((EVENTS / "plaid_webhook.json").read_text()), None)

    assert response["statusCode"] == 200
    t1 = get_txn(aws, "t1")
    assert t1["amount"] == Decimal("12.34")
    assert t1["status"] == "posted"
    assert t1["account_name"] == "Checking"
    assert t1["transaction_date"] == RECENT
    assert t1["plaid_category_primary"] == "FOOD_AND_DRINK"
    assert "authorized_date" not in t1  # nulls are omitted
    assert get_txn(aws, "t2")["status"] == "pending"


def test_initial_sync_without_row_limits_to_initial_days(aws, plaid):
    plaid.responses = [page(added=[txn("recent"), txn("old", txn_date=OLD)])]

    result = app.sync_item(ITEM_ID)

    assert plaid.calls == [{"cursor": None, "days_requested": 30}]
    assert get_txn(aws, "recent") is not None
    assert get_txn(aws, "old") is None
    assert result["skipped_before_start_date"] == 1
    row = get_plaid_item(aws)
    assert row["sync_start_date"] == (date.today() - timedelta(days=30)).isoformat()
    assert row["cursor"] == "cursor-1"
    assert "last_synced_at" in row
    assert row["last_sync_counts"]["upserted"] == 1


def test_incremental_sync_uses_cursor_and_keeps_start_date_filter(aws, plaid):
    start = (date.today() - timedelta(days=30)).isoformat()
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "cursor-1", "sync_start_date": start})
    plaid.responses = [page(modified=[txn("old", txn_date=OLD)], next_cursor="cursor-2")]

    app.sync_item(ITEM_ID)

    assert plaid.calls == [{"cursor": "cursor-1", "days_requested": None}]
    assert get_txn(aws, "old") is None
    assert get_plaid_item(aws)["cursor"] == "cursor-2"


def test_modified_preserves_user_fields(aws, plaid):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "cursor-1"})
    aws["transactions"].put_item(
        Item={
            "transaction_id": "t1",
            "amount": Decimal("1"),
            "expense_category": "Groceries",
            "telegram_message_id": 42,
            "created_at": "2020-01-01T00:00:00+00:00",
        }
    )
    plaid.responses = [page(modified=[txn("t1", amount=99.5)], next_cursor="cursor-2")]

    app.sync_item(ITEM_ID)

    t1 = get_txn(aws, "t1")
    assert t1["amount"] == Decimal("99.5")
    assert t1["expense_category"] == "Groceries"
    assert t1["telegram_message_id"] == 42
    assert t1["created_at"] == "2020-01-01T00:00:00+00:00"


def test_removed_transactions_are_deleted(aws, plaid):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "cursor-1"})
    aws["transactions"].put_item(Item={"transaction_id": "t1"})
    plaid.responses = [page(removed=["t1"], next_cursor="cursor-2")]

    app.sync_item(ITEM_ID)

    assert get_txn(aws, "t1") is None


def test_paginates_until_has_more_is_false(aws, plaid):
    plaid.responses = [
        page(added=[txn("t1")], next_cursor="c1", has_more=True),
        page(added=[txn("t2")], next_cursor="c2"),
    ]

    app.sync_item(ITEM_ID)

    assert [c["cursor"] for c in plaid.calls] == [None, "c1"]
    assert get_txn(aws, "t1") and get_txn(aws, "t2")
    assert get_plaid_item(aws)["cursor"] == "c2"


def test_restarts_pagination_on_mutation(aws, plaid):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "start"})
    plaid.responses = [
        page(added=[txn("stale")], next_cursor="c1", has_more=True),
        PlaidError("TRANSACTIONS_ERROR", app.MUTATION_DURING_PAGINATION, "changed"),
        page(added=[txn("t1")], next_cursor="c2"),
    ]

    app.sync_item(ITEM_ID)

    assert [c["cursor"] for c in plaid.calls] == ["start", "c1", "start"]
    assert get_txn(aws, "stale") is None
    assert get_txn(aws, "t1") is not None
    assert get_plaid_item(aws)["cursor"] == "c2"


def test_plaid_errors_propagate(aws, plaid):
    plaid.responses = [PlaidError("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "login")]

    with pytest.raises(PlaidError):
        app.sync_item(ITEM_ID)


def test_unknown_item_is_ignored(aws, plaid):
    response = app.lambda_handler(
        webhook_event(
            {
                "webhook_type": "TRANSACTIONS",
                "webhook_code": "SYNC_UPDATES_AVAILABLE",
                "item_id": "x",
            }
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert plaid.calls == []
    assert aws["items"].scan()["Items"] == []


@pytest.mark.parametrize(
    "webhook_code",
    ["SYNC_UPDATES_AVAILABLE", "INITIAL_UPDATE", "HISTORICAL_UPDATE", "DEFAULT_UPDATE"],
)
def test_transaction_update_webhooks_trigger_sync(aws, plaid, webhook_code):
    plaid.responses = [page(added=[txn("t1")])]

    response = app.lambda_handler(
        webhook_event(
            {"webhook_type": "TRANSACTIONS", "webhook_code": webhook_code, "item_id": ITEM_ID}
        ),
        None,
    )

    assert response["statusCode"] == 200
    assert len(plaid.calls) == 1
    assert get_txn(aws, "t1") is not None


def test_other_webhooks_do_not_sync(aws, plaid):
    response = app.lambda_handler(
        webhook_event({"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": ITEM_ID}), None
    )

    assert response["statusCode"] == 200
    assert plaid.calls == []


def test_invalid_body_returns_400(aws, plaid):
    response = app.lambda_handler(webhook_event("not json"), None)

    assert response["statusCode"] == 400


def test_direct_invoke_syncs_item(aws, plaid):
    plaid.responses = [page(added=[txn("t1")])]

    result = app.lambda_handler({"item_id": ITEM_ID}, None)

    assert result["synced"] is True
    assert result["upserted"] == 1
