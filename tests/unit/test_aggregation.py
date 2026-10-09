import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from aggregation import app
from shared import aws_secrets
from shared.categories import CATEGORIES

CHAT_ID = 555


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": 1, "chat": {"id": int(chat_id)}}


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
            AttributeDefinitions=[
                {"AttributeName": "transaction_id", "AttributeType": "S"},
                {"AttributeName": "expense_category", "AttributeType": "S"},
                {"AttributeName": "transaction_date", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "CategoryDateIndex",
                    "KeySchema": [
                        {"AttributeName": "expense_category", "KeyType": "HASH"},
                        {"AttributeName": "transaction_date", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        limits = dynamodb.create_table(
            TableName="limits",
            KeySchema=[{"AttributeName": "category", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "category", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        state = dynamodb.create_table(
            TableName="state",
            KeySchema=[
                {"AttributeName": "category", "KeyType": "HASH"},
                {"AttributeName": "month", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "category", "AttributeType": "S"},
                {"AttributeName": "month", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        secret_arn = boto3.client("secretsmanager").create_secret(
            Name="telegram", SecretString=json.dumps({"bot_token": "token"})
        )["ARN"]
        monkeypatch.setenv("TRANSACTIONS_TABLE", "transactions")
        monkeypatch.setenv("CATEGORY_LIMITS_TABLE", "limits")
        monkeypatch.setenv("CATEGORY_STATE_TABLE", "state")
        monkeypatch.setenv("TELEGRAM_SECRET_ARN", secret_arn)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
        monkeypatch.setattr(app, "_dynamodb", None)
        aws_secrets.clear_cache()
        yield {"transactions": transactions, "limits": limits, "state": state}
        aws_secrets.clear_cache()


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(app, "_telegram_client", lambda: fake)
    return fake


_txn_counter = 0


def put_txn(aws, category, date, amount):
    global _txn_counter
    _txn_counter += 1
    item = {
        "transaction_id": f"t{_txn_counter}",
        "transaction_date": date,
        "amount": Decimal(amount),
    }
    if category:
        item["expense_category"] = category
    aws["transactions"].put_item(Item=item)


def set_limit(aws, category, limit):
    aws["limits"].put_item(Item={"category": category, "limit": Decimal(limit)})


def get_state(aws, category, month):
    return aws["state"].get_item(Key={"category": category, "month": month}).get("Item")


def put_state(aws, category, month, limit, budget, expenses):
    aws["state"].put_item(
        Item={
            "category": category,
            "month": month,
            "limit": Decimal(limit),
            "budget": Decimal(budget),
            "current_expenses": Decimal(expenses),
        }
    )


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("month", "expected"),
    [("2026-10", "2026-09"), ("2026-01", "2025-12"), ("2026-12", "2026-11")],
)
def test_previous_month(month, expected):
    assert app.previous_month(month) == expected


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "october", ""])
def test_previous_month_rejects_invalid(month):
    with pytest.raises(ValueError):
        app.previous_month(month)


def prev_row(limit, budget, expenses):
    return {
        "limit": Decimal(limit),
        "budget": Decimal(budget),
        "current_expenses": Decimal(expenses),
    }


@pytest.mark.parametrize(
    ("limit", "prev", "expected"),
    [
        ("500", None, "500"),  # first month
        ("500", prev_row("-1", "-1", "300"), "500"),  # had no limit, now has one
        ("500", prev_row("500", "500", "200"), "800"),  # carry unspent remainder
        ("500", prev_row("500", "500", "600"), "400"),  # carry overspending
        ("500", prev_row("500", "-50", "0"), "450"),  # negative budget is not "no limit"
        ("-1", prev_row("500", "500", "100"), "-1"),  # limit removed
        ("-1", None, "-1"),
    ],
)
def test_compute_budget(limit, prev, expected):
    assert app.compute_budget(Decimal(limit), prev) == Decimal(expected)


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


def test_sums_only_the_months_categorized_transactions(aws):
    put_txn(aws, "essential", "2026-10-01", "10.50")
    put_txn(aws, "essential", "2026-10-31", "20")
    put_txn(aws, "essential", "2026-09-30", "100")  # previous month
    put_txn(aws, "essential", "2026-11-01", "100")  # next month
    put_txn(aws, "hobby", "2026-10-05", "7")
    put_txn(aws, None, "2026-10-05", "1000")  # uncategorized

    assert app.month_expenses("essential", "2026-10") == Decimal("30.50")
    assert app.month_expenses("hobby", "2026-10") == Decimal("7")
    assert app.month_expenses("sport", "2026-10") == Decimal("0")


def test_first_run_creates_rows_for_all_categories(aws):
    set_limit(aws, "essential", "1000")
    put_txn(aws, "essential", "2026-10-02", "250")

    rows = app.aggregate("2026-10")

    assert [row["category"] for row in rows] == list(CATEGORIES)
    essential = get_state(aws, "essential", "2026-10")
    assert essential["limit"] == Decimal("1000")
    assert essential["budget"] == Decimal("1000")
    assert essential["current_expenses"] == Decimal("250")
    hobby = get_state(aws, "hobby", "2026-10")  # no limit row
    assert hobby["limit"] == Decimal("-1")
    assert hobby["budget"] == Decimal("-1")
    assert get_state(aws, "essential", "2026-09") is None


def test_rerun_is_idempotent_and_picks_up_new_transactions(aws):
    set_limit(aws, "essential", "1000")
    put_txn(aws, "essential", "2026-10-02", "250")
    app.aggregate("2026-10")
    app.aggregate("2026-10")
    assert get_state(aws, "essential", "2026-10")["current_expenses"] == Decimal("250")

    put_txn(aws, "essential", "2026-10-03", "50")
    app.aggregate("2026-10")

    row = get_state(aws, "essential", "2026-10")
    assert row["current_expenses"] == Decimal("300")
    assert row["budget"] == Decimal("1000")


def test_limit_change_applies_from_next_month(aws):
    set_limit(aws, "essential", "1000")
    app.aggregate("2026-10")

    set_limit(aws, "essential", "2000")
    app.aggregate("2026-10")
    assert get_state(aws, "essential", "2026-10")["budget"] == Decimal("1000")

    app.aggregate("2026-11")
    assert get_state(aws, "essential", "2026-11")["budget"] == Decimal("3000")


def test_rollover_carries_remainder(aws):
    set_limit(aws, "essential", "1000")
    set_limit(aws, "hobby", "100")
    put_state(aws, "essential", "2026-09", "1000", "1000", "0")
    put_state(aws, "hobby", "2026-09", "100", "100", "0")
    put_txn(aws, "essential", "2026-09-10", "800")
    put_txn(aws, "hobby", "2026-09-10", "150")

    app.aggregate("2026-10")

    assert get_state(aws, "essential", "2026-09")["current_expenses"] == Decimal("800")
    assert get_state(aws, "essential", "2026-10")["budget"] == Decimal("1200")
    assert get_state(aws, "hobby", "2026-10")["budget"] == Decimal("50")


def test_late_transaction_updates_previous_month_and_current_budget(aws):
    set_limit(aws, "essential", "1000")
    put_txn(aws, "essential", "2026-09-10", "800")
    app.aggregate("2026-09")
    app.aggregate("2026-10")
    assert get_state(aws, "essential", "2026-10")["budget"] == Decimal("1200")

    put_txn(aws, "essential", "2026-09-30", "100")  # categorized in October
    app.aggregate("2026-10")

    assert get_state(aws, "essential", "2026-09")["current_expenses"] == Decimal("900")
    assert get_state(aws, "essential", "2026-10")["budget"] == Decimal("1100")


def test_limit_transitions(aws):
    put_state(aws, "essential", "2026-09", "-1", "-1", "500")  # had no limit
    put_state(aws, "hobby", "2026-09", "100", "100", "20")  # had a limit
    set_limit(aws, "essential", "1000")
    set_limit(aws, "hobby", "-1")

    app.aggregate("2026-10")

    assert get_state(aws, "essential", "2026-10")["budget"] == Decimal("1000")
    assert get_state(aws, "hobby", "2026-10")["budget"] == Decimal("-1")


# ----------------------------------------------------------------------
# Report and handler
# ----------------------------------------------------------------------


def test_format_report():
    rows = [
        {
            "category": "essential",
            "limit": Decimal("1000"),
            "budget": Decimal("1000"),
            "current_expenses": Decimal("812.4"),
        },
        {
            "category": "hobby",
            "limit": Decimal("100"),
            "budget": Decimal("100"),
            "current_expenses": Decimal("120"),
        },
        {
            "category": "sport",
            "limit": Decimal("-1"),
            "budget": Decimal("-1"),
            "current_expenses": Decimal("45"),
        },
    ]

    text = app.format_report("2026-10", rows)

    assert text.splitlines() == [
        "📊 <b>Budget · October 2026</b>",
        "",
        "Essential: 812.40 / 1,000.00 · 187.60 left",
        "Hobby: 120.00 / 100.00 · ⚠️ 20.00 over",
        "Sport: 45.00 · no limit",
        "",
        "<b>Total spent: 977.40</b>",
    ]


def test_handler_sends_report(aws, telegram):
    set_limit(aws, "essential", "1000")
    put_txn(aws, "essential", "2026-10-02", "250")

    result = app.lambda_handler({"month": "2026-10"}, None)

    assert len(telegram.sent) == 1
    assert telegram.sent[0]["chat_id"] == str(CHAT_ID)
    assert "Essential: 250.00 / 1,000.00 · 750.00 left" in telegram.sent[0]["text"]
    assert result["month"] == "2026-10"
    assert result["categories"][0] == {
        "category": "essential",
        "limit": "1000",
        "budget": "1000",
        "current_expenses": "250",
    }
    json.dumps(result)  # Lambda return value must be JSON serializable


def test_handler_defaults_to_current_month(aws, telegram):
    result = app.lambda_handler({}, None)
    assert app.MONTH_PATTERN.match(result["month"])
