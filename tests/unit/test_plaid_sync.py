import base64
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
from shared.telegram import TelegramError

EVENTS = Path(__file__).parents[2] / "events"
ITEM_ID = "sandbox-item-id"
RECENT = (date.today() - timedelta(days=2)).isoformat()
OLD = (date.today() - timedelta(days=60)).isoformat()
OLDER_IN_RANGE = (date.today() - timedelta(days=5)).isoformat()


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


@pytest.fixture(autouse=True)
def webhook_runtime(monkeypatch):
    """Accept webhook signatures, run the async sync inline, and capture Telegram alerts."""
    runtime = {"verified": True, "started": [], "alerts": []}
    monkeypatch.setattr(app.plaid_webhook, "verify", lambda client, h, b: runtime["verified"])

    def start_sync(item_id):
        runtime["started"].append(item_id)
        app.sync_item(item_id)

    monkeypatch.setattr(app, "_start_sync", start_sync)
    monkeypatch.setattr(app, "_telegram_alert", runtime["alerts"].append)
    return runtime


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


def test_posted_transaction_inherits_category_from_pending(aws, plaid):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "cursor-1"})
    aws["transactions"].put_item(
        Item={
            "transaction_id": "pending-1",
            "status": "pending",
            "expense_category": "essential",
            "categorized_by": 123,
            "telegram_message_id": 42,
            "telegram_chat_id": 555,
        }
    )
    plaid.responses = [
        page(
            added=[txn("posted-1", pending_transaction_id="pending-1")],
            removed=["pending-1"],
            next_cursor="cursor-2",
        )
    ]

    app.sync_item(ITEM_ID)

    posted = get_txn(aws, "posted-1")
    assert posted["status"] == "posted"
    assert posted["expense_category"] == "essential"
    assert posted["categorized_by"] == 123
    assert posted["telegram_message_id"] == 42
    assert posted["telegram_chat_id"] == 555
    assert get_txn(aws, "pending-1") is None


def test_carry_over_does_not_overwrite_posted_fields(aws, plaid):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "cursor-1"})
    aws["transactions"].put_item(
        Item={"transaction_id": "pending-1", "expense_category": "hobby"}
    )
    aws["transactions"].put_item(
        Item={"transaction_id": "posted-1", "expense_category": "essential"}
    )
    plaid.responses = [
        page(modified=[txn("posted-1", pending_transaction_id="pending-1")], next_cursor="c2")
    ]

    app.sync_item(ITEM_ID)

    assert get_txn(aws, "posted-1")["expense_category"] == "essential"


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


def test_transient_plaid_errors_propagate_for_retry(aws, plaid):
    plaid.responses = [PlaidError("INSTITUTION_ERROR", "INSTITUTION_DOWN", "down")]

    with pytest.raises(PlaidError):
        app.sync_item(ITEM_ID)


def test_login_required_during_sync_marks_item_and_alerts_once(aws, plaid, webhook_runtime):
    aws["items"].put_item(
        Item={"item_id": ITEM_ID, "cursor": "c1", "institution_name": "Chase"}
    )
    error = PlaidError("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "login")
    plaid.responses = [error, error]

    first = app.sync_item(ITEM_ID)
    app.sync_item(ITEM_ID)

    assert first == {"item_id": ITEM_ID, "synced": False, "error_code": "ITEM_LOGIN_REQUIRED"}
    row = get_plaid_item(aws)
    assert row["status"] == "needs_update"
    assert row["status_reason"] == "ITEM_LOGIN_REQUIRED"
    assert row["cursor"] == "c1"
    assert len(webhook_runtime["alerts"]) == 1
    assert "Chase" in webhook_runtime["alerts"][0]
    assert f"plaid_link.py update {ITEM_ID}" in webhook_runtime["alerts"][0]


def test_successful_sync_clears_needs_update(aws, plaid, webhook_runtime):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "c1", "status": "needs_update"})
    plaid.responses = [page(next_cursor="c2")]

    app.sync_item(ITEM_ID)

    assert get_plaid_item(aws)["status"] == "ok"
    assert webhook_runtime["alerts"] == [app.alert_text({"item_id": ITEM_ID, "status": "ok"})]


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


def test_sync_webhook_hands_off_to_async_sync(aws, plaid, webhook_runtime):
    plaid.responses = [page()]

    app.lambda_handler(json.loads((EVENTS / "plaid_webhook.json").read_text()), None)

    assert webhook_runtime["started"] == [ITEM_ID]


def test_unverified_webhook_is_rejected(aws, plaid, webhook_runtime):
    webhook_runtime["verified"] = False

    response = app.lambda_handler(json.loads((EVENTS / "plaid_webhook.json").read_text()), None)

    assert response["statusCode"] == 401
    assert webhook_runtime["started"] == []


def test_base64_body_is_decoded(aws, plaid, webhook_runtime):
    event = json.loads((EVENTS / "plaid_webhook.json").read_text())
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    plaid.responses = [page()]

    assert app.lambda_handler(event, None)["statusCode"] == 200
    assert webhook_runtime["started"] == [ITEM_ID]


def item_webhook(code, **extra):
    body = {"webhook_type": "ITEM", "webhook_code": code, "item_id": ITEM_ID, **extra}
    return webhook_event(body)


def test_other_webhooks_do_not_sync(aws, plaid):
    response = app.lambda_handler(item_webhook("WEBHOOK_UPDATE_ACKNOWLEDGED"), None)

    assert response["statusCode"] == 200
    assert plaid.calls == []


def test_item_error_webhook_alerts_once_per_change(aws, plaid, webhook_runtime):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "institution_name": "Bank <&>"})
    event = item_webhook("ERROR", error={"error_code": "ITEM_LOGIN_REQUIRED"})

    assert app.lambda_handler(event, None)["statusCode"] == 200
    app.lambda_handler(event, None)  # redelivered

    assert get_plaid_item(aws)["status"] == "needs_update"
    assert len(webhook_runtime["alerts"]) == 1
    assert "Bank &lt;&amp;&gt;" in webhook_runtime["alerts"][0]
    assert plaid.calls == []


@pytest.mark.parametrize(
    ("code", "extra", "status"),
    [
        ("PENDING_DISCONNECT", {}, "pending_disconnect"),
        ("PENDING_EXPIRATION", {}, "pending_disconnect"),
        ("USER_PERMISSION_REVOKED", {}, "revoked"),
        ("ERROR", {"error": {"error_code": "ITEM_NOT_FOUND"}}, "revoked"),
        ("NEW_ACCOUNTS_AVAILABLE", {}, "new_accounts"),
    ],
)
def test_item_webhooks_set_status(aws, plaid, webhook_runtime, code, extra, status):
    aws["items"].put_item(Item={"item_id": ITEM_ID})

    app.lambda_handler(item_webhook(code, **extra), None)

    assert get_plaid_item(aws)["status"] == status
    assert len(webhook_runtime["alerts"]) == 1


def test_login_repaired_restores_ok(aws, plaid, webhook_runtime):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "status": "needs_update"})

    app.lambda_handler(item_webhook("LOGIN_REPAIRED"), None)

    assert get_plaid_item(aws)["status"] == "ok"
    assert "connected again" in webhook_runtime["alerts"][0]


def test_new_accounts_does_not_mask_a_broken_login(aws, plaid, webhook_runtime):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "status": "needs_update"})

    app.lambda_handler(item_webhook("NEW_ACCOUNTS_AVAILABLE"), None)

    assert get_plaid_item(aws)["status"] == "needs_update"
    assert webhook_runtime["alerts"] == []


def test_item_webhook_for_unknown_item_is_ignored(aws, plaid, webhook_runtime):
    app.lambda_handler(item_webhook("ERROR", error={"error_code": "ITEM_LOGIN_REQUIRED"}), None)

    assert aws["items"].scan()["Items"] == []
    assert webhook_runtime["alerts"] == []


def test_invalid_body_returns_400(aws, plaid):
    response = app.lambda_handler(webhook_event("not json"), None)

    assert response["statusCode"] == 400


def test_direct_invoke_syncs_item(aws, plaid):
    plaid.responses = [page(added=[txn("t1")])]

    result = app.lambda_handler({"item_id": ITEM_ID}, None)

    assert result["synced"] is True
    assert result["upserted"] == 1


# ----------------------------------------------------------------------
# Telegram notifications for new expenses
# ----------------------------------------------------------------------


class FakeTelegram:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise TelegramError("Too Many Requests", 429)
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": 100 + len(self.sent), "chat": {"id": int(chat_id)}}


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setattr(app, "_telegram_client", lambda: fake)
    return fake


def sent_titles(telegram):
    return [m["text"].split("\n")[0] for m in telegram.sent]


def test_sync_sends_added_expenses_to_telegram(aws, plaid, telegram):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "c1"})
    plaid.responses = [
        page(
            added=[
                txn("t2", merchant_name="Second", txn_date=RECENT),
                txn("t1", merchant_name="First", txn_date=OLDER_IN_RANGE),
            ],
            next_cursor="c2",
        )
    ]

    result = app.lambda_handler({"item_id": ITEM_ID}, None)

    assert result["telegram_sent"] == 2
    assert sent_titles(telegram) == ["<b>First</b>", "<b>Second</b>"]
    assert telegram.sent[0]["reply_markup"]["inline_keyboard"]
    assert get_txn(aws, "t1")["telegram_message_id"] == 101
    assert get_txn(aws, "t1")["telegram_chat_id"] == 555
    assert get_plaid_item(aws)["last_sync_counts"]["upserted"] == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"amount": -50},
        {"personal_finance_category": {"primary": "TRANSFER_OUT", "detailed": "X"}},
        {"personal_finance_category": {"primary": "INCOME", "detailed": "INCOME_WAGES"}},
        {
            "personal_finance_category": {
                "primary": "LOAN_PAYMENTS",
                "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            }
        },
    ],
)
def test_non_expenses_are_not_sent(aws, plaid, telegram, extra):
    plaid.responses = [page(added=[txn("t1", **extra)])]

    result = app.sync_item(ITEM_ID)

    assert telegram.sent == []
    assert result["telegram_sent"] == 0


def test_modified_transactions_are_not_sent(aws, plaid, telegram):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "c1"})
    plaid.responses = [page(modified=[txn("t1")], next_cursor="c2")]

    app.sync_item(ITEM_ID)

    assert telegram.sent == []


def test_posted_transaction_of_sent_pending_is_not_resent(aws, plaid, telegram):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "c1"})
    plaid.responses = [page(added=[txn("pending-1", pending=True)], next_cursor="c2")]
    app.sync_item(ITEM_ID)
    assert len(telegram.sent) == 1

    plaid.responses = [
        page(
            added=[txn("posted-1", pending_transaction_id="pending-1")],
            removed=["pending-1"],
            next_cursor="c3",
        )
    ]
    result = app.sync_item(ITEM_ID)

    assert len(telegram.sent) == 1
    assert result["telegram_sent"] == 0
    assert get_txn(aws, "posted-1")["telegram_message_id"] == 101


def test_added_then_removed_in_same_sync_is_not_sent(aws, plaid, telegram):
    plaid.responses = [page(added=[txn("t1")], removed=["t1"])]

    app.sync_item(ITEM_ID)

    assert telegram.sent == []
    assert get_txn(aws, "t1") is None


def test_run_that_loses_cursor_lock_sends_nothing(aws, plaid, telegram):
    aws["items"].put_item(Item={"item_id": ITEM_ID, "cursor": "c1"})
    plaid.responses = [page(added=[txn("t1")], next_cursor="c2")]
    original = plaid.transactions_sync

    def concurrent_sync_wins(*args, **kwargs):
        response = original(*args, **kwargs)
        # A duplicate webhook's sync saves the same page's cursor first.
        aws["items"].update_item(
            Key={"item_id": ITEM_ID},
            UpdateExpression="SET #c = :c",
            ExpressionAttributeNames={"#c": "cursor"},
            ExpressionAttributeValues={":c": "c2"},
        )
        return response

    plaid.transactions_sync = concurrent_sync_wins

    result = app.sync_item(ITEM_ID)

    assert telegram.sent == []
    assert "telegram_sent" not in result


def test_max_messages_per_sync(aws, plaid, telegram, monkeypatch):
    monkeypatch.setenv("MAX_MESSAGES_PER_SYNC", "2")
    plaid.responses = [page(added=[txn(f"t{i}", merchant_name=f"M{i}") for i in range(4)])]

    result = app.sync_item(ITEM_ID)

    assert result["telegram_sent"] == 2
    assert sent_titles(telegram) == ["<b>M0</b>", "<b>M1</b>"]
    assert "telegram_message_id" not in get_txn(aws, "t3")


def test_telegram_error_does_not_fail_sync(aws, plaid, telegram):
    telegram.fail_after = 1
    plaid.responses = [page(added=[txn("t1"), txn("t2")])]

    result = app.sync_item(ITEM_ID)

    assert result["synced"] is True
    assert result["telegram_sent"] == 1
    assert get_plaid_item(aws)["cursor"] == "cursor-1"
    assert "telegram_message_id" not in get_txn(aws, "t2")


def test_no_chat_id_skips_sending(aws, plaid, telegram, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    plaid.responses = [page(added=[txn("t1")])]

    result = app.sync_item(ITEM_ID)

    assert telegram.sent == []
    assert result["telegram_sent"] == 0
