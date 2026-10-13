import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from apple_card_import import app
from shared import aws_secrets

CHAT_ID = 555
USER_ID = 123456789
HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,"
    "Amount (USD),Purchased By"
)
PARKING = (
    '10/04/2026,10/05/2026,"SC WHARF GATE SANTA CRUZ CA USA","Sc Wharf Gate",'
    '"Govt-services-parking","Purchase","4.00","Alex Doe"'
)
PAYMENT = (
    '10/04/2026,10/04/2026,"ACH DEPOSIT INTERNET TRANSFER","Ach Deposit",'
    '"Payment","Payment","-290.97","Alex Doe"'
)
APPLE = (
    '10/01/2026,10/02/2026,"APPLE.COM/BILL CUPERTINO CA USA","Apple Services",'
    '"Other","Purchase","2.99","Alex Doe"'
)
DINNER_PENDING = (
    '10/06/2026,,"TACO SHOP SANTA CRUZ CA USA","Taco Shop","Restaurants","Purchase",'
    '"20.00","Sam Doe"'
)
DINNER_POSTED = (
    '10/06/2026,10/07/2026,"TACO SHOP SANTA CRUZ CA USA","Taco Shop","Restaurants",'
    '"Purchase","24.00","Sam Doe"'
)


def csv_bytes(*lines, bom=False):
    text = "\n".join([HEADER, *lines]) + "\n"
    return (("﻿" if bom else "") + text).encode()


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.files = {}
        self.next_message_id = 100

    def get_file(self, file_id):
        return {"file_id": file_id, "file_path": f"documents/{file_id}.csv"}

    def download_file(self, file_path, max_bytes):
        return self.files[file_path.removeprefix("documents/").removesuffix(".csv")]

    def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "reply_to": reply_to_message_id,
            }
        )
        self.next_message_id += 1
        return {"message_id": self.next_message_id, "chat": {"id": int(chat_id)}}

    def summaries(self):
        return [m for m in self.sent if m["reply_markup"] is None]

    def expenses(self):
        return [m for m in self.sent if m["reply_markup"] is not None]


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        transactions = boto3.resource("dynamodb").create_table(
            TableName="transactions",
            KeySchema=[{"AttributeName": "transaction_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "transaction_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        secret_arn = boto3.client("secretsmanager").create_secret(
            Name="telegram", SecretString=json.dumps({"bot_token": "token"})
        )["ARN"]
        monkeypatch.setenv("TRANSACTIONS_TABLE", "transactions")
        monkeypatch.setenv("TELEGRAM_SECRET_ARN", secret_arn)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
        monkeypatch.setenv("MAX_MESSAGES_PER_IMPORT", "20")
        monkeypatch.setattr(app, "_dynamodb", None)
        aws_secrets.clear_cache()
        yield transactions
        aws_secrets.clear_cache()


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(app, "_telegram_client", lambda: fake)
    return fake


def run_import(telegram, content, file_id="f1"):
    telegram.files[file_id] = content
    event = {
        "file_id": file_id,
        "file_name": "Apple Card Transactions.csv",
        "chat_id": CHAT_ID,
        "message_id": 42,
        "user_id": USER_ID,
    }
    return app.lambda_handler(event, None)


def all_rows(table):
    return sorted(table.scan()["Items"], key=lambda item: item["transaction_date"])


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def test_parse_csv_maps_columns():
    rows = app.parse_csv(csv_bytes(PARKING, DINNER_PENDING, bom=True))

    assert rows[0] == {
        "transaction_date": "2026-10-04",
        "clearing_date": "2026-10-05",
        "status": "posted",
        "name": "SC WHARF GATE SANTA CRUZ CA USA",
        "merchant_name": "Sc Wharf Gate",
        "apple_category": "Govt-services-parking",
        "apple_type": "Purchase",
        "amount": Decimal("4.00"),
        "currency": "USD",
        "purchased_by": "Alex Doe",
        "account_name": "Apple Card",
        "source": "apple_card",
    }
    assert rows[1]["status"] == "pending"
    assert "clearing_date" not in rows[1]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"Date,Amount\n01/01/2026,1.00\n", "missing columns"),
        (csv_bytes(), "no transactions"),
        (csv_bytes(PARKING.replace('"4.00"', '"abc"')), "bad amount"),
        (csv_bytes(PARKING.replace("10/04/2026", "2026-10-04", 1)), "bad Transaction Date"),
        (b"\xff\xfe\x00", "UTF-8"),
    ],
)
def test_parse_csv_rejects_bad_files(content, message):
    with pytest.raises(app.CsvFormatError, match=message):
        app.parse_csv(content)


# ----------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------


def test_first_import_stores_rows_and_sends_expenses(table, telegram):
    result = run_import(telegram, csv_bytes(PARKING, PAYMENT, APPLE))

    assert result["new"] == 3
    rows = all_rows(table)
    assert [r["transaction_id"].startswith("apple-") for r in rows] == [True] * 3
    assert {r["import_file_name"] for r in rows} == {"Apple Card Transactions.csv"}
    assert {r["imported_by"] for r in rows} == {USER_ID}
    # The payment is stored but never sent for categorization.
    assert [m["text"].split("\n")[0] for m in telegram.expenses()] == [
        "<b>Apple Services</b>",
        "<b>Sc Wharf Gate</b>",
    ]
    assert "By Alex Doe\nApple: Govt-services-parking" in telegram.expenses()[1]["text"]
    assert telegram.summaries() == [
        {
            "chat_id": CHAT_ID,
            "text": "🍏 Apple Card import: 3 new, 0 updated, 0 unchanged · "
            "2 sent for categorization",
            "reply_markup": None,
            "reply_to": 42,
        }
    ]


def test_reimporting_the_same_file_changes_nothing(table, telegram):
    run_import(telegram, csv_bytes(PARKING, PAYMENT, APPLE))
    before = all_rows(table)
    telegram.sent.clear()

    result = run_import(telegram, csv_bytes(PARKING, PAYMENT, APPLE), file_id="f2")

    assert (result["new"], result["updated"], result["unchanged"]) == (0, 0, 3)
    assert all_rows(table) == before
    assert telegram.expenses() == []


def test_overlapping_file_adds_new_rows_and_posts_pending(table, telegram):
    run_import(telegram, csv_bytes(APPLE, DINNER_PENDING.replace('"20.00"', '"24.00"')))
    pending = next(r for r in all_rows(table) if r["merchant_name"] == "Taco Shop")
    assert pending["status"] == "pending"
    telegram.sent.clear()

    result = run_import(telegram, csv_bytes(APPLE, DINNER_POSTED, PARKING), file_id="f2")

    assert (result["new"], result["updated"], result["unchanged"]) == (1, 1, 1)
    posted = table.get_item(Key={"transaction_id": pending["transaction_id"]})["Item"]
    assert posted["status"] == "posted"
    assert posted["clearing_date"] == "2026-10-07"
    assert posted["telegram_message_id"] == pending["telegram_message_id"]
    # Only the new parking row is sent.
    assert len(telegram.expenses()) == 1
    assert len(all_rows(table)) == 3


def test_amount_change_updates_in_place_and_keeps_category(table, telegram):
    run_import(telegram, csv_bytes(DINNER_PENDING))
    [row] = all_rows(table)
    table.update_item(
        Key={"transaction_id": row["transaction_id"]},
        UpdateExpression="SET expense_category = :c",
        ExpressionAttributeValues={":c": "weekend_fun"},
    )
    telegram.sent.clear()

    result = run_import(telegram, csv_bytes(DINNER_POSTED), file_id="f2")

    assert result["updated"] == 1
    [updated] = all_rows(table)
    assert updated["transaction_id"] == row["transaction_id"]
    assert updated["amount"] == Decimal("24.00")
    assert updated["expense_category"] == "weekend_fun"
    assert telegram.expenses() == []


def test_identical_rows_in_one_file_are_kept_apart(table, telegram):
    run_import(telegram, csv_bytes(PARKING, PARKING))
    assert len({r["transaction_id"] for r in all_rows(table)}) == 2

    result = run_import(telegram, csv_bytes(PARKING, PARKING, PARKING), file_id="f2")

    assert (result["new"], result["unchanged"]) == (1, 2)
    assert len(all_rows(table)) == 3


def test_new_row_does_not_reuse_id_of_row_whose_amount_changed(table, telegram):
    run_import(telegram, csv_bytes(DINNER_PENDING))
    run_import(telegram, csv_bytes(DINNER_POSTED), file_id="f2")

    # A second, separate charge at the original amount on the same day.
    result = run_import(telegram, csv_bytes(DINNER_POSTED, DINNER_PENDING), file_id="f3")

    assert (result["new"], result["unchanged"]) == (1, 1)
    assert sorted(r["amount"] for r in all_rows(table)) == [Decimal("20.00"), Decimal("24.00")]


def test_rows_missing_from_a_later_file_are_kept(table, telegram):
    run_import(telegram, csv_bytes(APPLE, PARKING))

    run_import(telegram, csv_bytes(APPLE), file_id="f2")

    assert len(all_rows(table)) == 2


def test_plaid_rows_are_not_matched(table, telegram):
    table.put_item(
        Item={
            "transaction_id": "plaid-1",
            "transaction_date": "2026-10-04",
            "name": "SC WHARF GATE SANTA CRUZ CA USA",
            "amount": Decimal("4.00"),
            "purchased_by": "Alex Doe",
        }
    )

    result = run_import(telegram, csv_bytes(PARKING))

    assert result["new"] == 1


def test_bad_file_gets_an_error_reply(table, telegram):
    result = run_import(telegram, b"Date,Amount\n01/01/2026,1.00\n")

    assert result["imported"] is False
    [reply] = telegram.sent
    assert reply["text"].startswith("🍏 This doesn't look like an Apple Card export")
    assert reply["reply_to"] == 42
    assert all_rows(table) == []


def test_unexpected_failure_replies_and_raises(table, telegram, monkeypatch):
    def fail(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "import_rows", fail)

    with pytest.raises(RuntimeError):
        run_import(telegram, csv_bytes(PARKING))
    assert telegram.sent[0]["text"] == "🍏 Apple Card import failed. Please try again later."
