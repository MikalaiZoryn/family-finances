import json
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shared import aws_secrets
from shared.telegram import TelegramError
from telegram_bot import app

EVENTS = Path(__file__).parents[2] / "events"
WEBHOOK_SECRET = "local-test-secret"
CHAT_ID = 555
USER_ID = 123456789


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.answers = []
        self.edits = []
        self.next_message_id = 100

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        self.next_message_id += 1
        return {"message_id": self.next_message_id, "chat": {"id": int(chat_id)}}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append({"id": callback_query_id, "text": text})
        return True

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "markup": reply_markup}
        )
        return True


@pytest.fixture
def aws(monkeypatch):
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
            Name="telegram",
            SecretString=json.dumps(
                {"bot_token": "token", "webhook_secret_token": WEBHOOK_SECRET}
            ),
        )["ARN"]
        monkeypatch.setenv("TRANSACTIONS_TABLE", "transactions")
        monkeypatch.setenv("TELEGRAM_SECRET_ARN", secret_arn)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT_ID))
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", f"{USER_ID}, 42")
        monkeypatch.setenv("MAX_MESSAGES_PER_RUN", "20")
        monkeypatch.setattr(app, "_dynamodb", None)
        aws_secrets.clear_cache()
        yield transactions
        aws_secrets.clear_cache()


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(app, "_telegram_client", lambda: fake)
    return fake


def put_txn(table, transaction_id, **overrides):
    item = {
        "transaction_id": transaction_id,
        "amount": Decimal("12.34"),
        "currency": "USD",
        "transaction_date": "2026-10-05",
        "status": "posted",
        "name": "STARBUCKS #123",
        "merchant_name": "Starbucks",
        "account_name": "Checking",
        "account_mask": "0000",
        "plaid_category_primary": "FOOD_AND_DRINK",
        "plaid_category_detailed": "FOOD_AND_DRINK_COFFEE",
        **overrides,
    }
    table.put_item(Item={k: v for k, v in item.items() if v is not None})


def get_txn(table, transaction_id):
    return table.get_item(Key={"transaction_id": transaction_id}).get("Item")


def callback_event(data, user_id=USER_ID, secret=WEBHOOK_SECRET):
    event = json.loads((EVENTS / "telegram_update.json").read_text())
    if secret is None:
        del event["headers"]["x-telegram-bot-api-secret-token"]
    else:
        event["headers"]["x-telegram-bot-api-secret-token"] = secret
    event["body"] = json.dumps(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "message": {"message_id": 101, "chat": {"id": CHAT_ID}, "text": "..."},
                "data": data,
            },
        }
    )
    return event


# ----------------------------------------------------------------------
# is_categorizable
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"amount": Decimal("5")}, True),
        ({"amount": Decimal("-5")}, False),
        ({"amount": Decimal("0")}, False),
        ({"amount": Decimal("5"), "plaid_category_primary": "TRANSFER_OUT"}, False),
        ({"amount": Decimal("5"), "plaid_category_primary": "TRANSFER_IN"}, False),
        ({"amount": Decimal("5"), "plaid_category_primary": "INCOME"}, False),
        (
            {
                "amount": Decimal("5"),
                "plaid_category_primary": "LOAN_PAYMENTS",
                "plaid_category_detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            },
            False,
        ),
        (
            {
                "amount": Decimal("5"),
                "plaid_category_primary": "LOAN_PAYMENTS",
                "plaid_category_detailed": "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
            },
            True,
        ),
    ],
)
def test_is_categorizable(item, expected):
    assert app.is_categorizable(item) is expected


# ----------------------------------------------------------------------
# Sending
# ----------------------------------------------------------------------


def test_sends_only_uncategorized_expenses(aws, telegram):
    put_txn(aws, "posted")
    put_txn(aws, "pending", status="pending", transaction_date="2026-10-06")
    put_txn(aws, "refund", amount=Decimal("-10"))
    put_txn(aws, "transfer", plaid_category_primary="TRANSFER_OUT")
    put_txn(aws, "payroll", amount=Decimal("-2000"), plaid_category_primary="INCOME")
    put_txn(
        aws,
        "card-payment",
        plaid_category_primary="LOAN_PAYMENTS",
        plaid_category_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    put_txn(aws, "categorized", expense_category="hobby")
    put_txn(aws, "already-sent", telegram_message_id=1)

    result = app.lambda_handler({}, None)

    sent_ids = [
        m["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
        for m in telegram.sent
    ]
    assert sent_ids == ["posted", "pending"]  # oldest first
    assert result == {"candidates": 4, "excluded": 2, "sent": 2, "remaining": 0}
    assert all(m["chat_id"] == str(CHAT_ID) for m in telegram.sent)
    posted = get_txn(aws, "posted")
    assert posted["telegram_message_id"] == 101
    assert posted["telegram_chat_id"] == CHAT_ID
    assert "telegram_sent_at" in posted


def test_second_run_sends_nothing(aws, telegram):
    put_txn(aws, "t1")

    app.send_uncategorized()
    result = app.send_uncategorized()

    assert len(telegram.sent) == 1
    assert result["sent"] == 0


def test_message_content_and_buttons(aws, telegram):
    put_txn(aws, "t1", status="pending")

    app.send_uncategorized()

    message = telegram.sent[0]
    text = message["text"]
    assert "<b>Starbucks</b>" in text
    assert "12.34 USD" in text
    assert "2026-10-05" in text
    assert "Pending" in text
    assert "Checking ••0000" in text
    assert message["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Essential", "callback_data": "cat:t1:essential"}],
            [{"text": "Weekend Fun", "callback_data": "cat:t1:weekend_fun"}],
            [{"text": "Hobby", "callback_data": "cat:t1:hobby"}],
        ]
    }


def test_message_escapes_html(aws, telegram):
    put_txn(aws, "t1", merchant_name="A&B <Shop>")

    app.send_uncategorized()

    assert "<b>A&amp;B &lt;Shop&gt;</b>" in telegram.sent[0]["text"]


def test_max_messages_per_run(aws, telegram, monkeypatch):
    monkeypatch.setenv("MAX_MESSAGES_PER_RUN", "2")
    for i in range(5):
        put_txn(aws, f"t{i}", transaction_date=f"2026-10-0{i + 1}")

    result = app.send_uncategorized()

    assert result["sent"] == 2
    assert result["remaining"] == 3
    assert get_txn(aws, "t0").get("telegram_message_id")
    assert not get_txn(aws, "t4").get("telegram_message_id")


def test_too_long_transaction_id_is_skipped(aws, telegram):
    put_txn(aws, "x" * 60)

    result = app.send_uncategorized()

    assert telegram.sent == []
    assert result["sent"] == 0


# ----------------------------------------------------------------------
# Webhook
# ----------------------------------------------------------------------


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_webhook_rejects_bad_secret_token(aws, telegram, secret):
    put_txn(aws, "t1")

    response = app.lambda_handler(callback_event("cat:t1:essential", secret=secret), None)

    assert response["statusCode"] == 401
    assert "expense_category" not in get_txn(aws, "t1")
    assert telegram.answers == []


def test_webhook_invalid_body_returns_400(aws, telegram):
    event = callback_event("cat:t1:essential")
    event["body"] = "not json"

    response = app.lambda_handler(event, None)

    assert response["statusCode"] == 400


def test_callback_saves_category(aws, telegram):
    put_txn(aws, "t1")

    response = app.lambda_handler(callback_event("cat:t1:weekend_fun"), None)

    assert response["statusCode"] == 200
    item = get_txn(aws, "t1")
    assert item["expense_category"] == "weekend_fun"
    assert item["categorized_by"] == USER_ID
    assert "categorized_at" in item
    assert telegram.answers == [{"id": "cb-1", "text": "Saved: Weekend Fun"}]
    edit = telegram.edits[0]
    assert (edit["chat_id"], edit["message_id"], edit["markup"]) == (CHAT_ID, 101, None)
    assert "<b>Starbucks</b>" in edit["text"]
    assert "Category: <b>Weekend Fun</b>" in edit["text"]


def test_callback_from_disallowed_user(aws, telegram):
    put_txn(aws, "t1")

    response = app.lambda_handler(callback_event("cat:t1:essential", user_id=999), None)

    assert response["statusCode"] == 200
    assert "expense_category" not in get_txn(aws, "t1")
    assert telegram.answers == [{"id": "cb-1", "text": "Not allowed"}]
    assert telegram.edits == []


@pytest.mark.parametrize("data", ["cat:t1:groceries", "cat:t1", "other:t1:essential", ""])
def test_callback_with_unknown_data(aws, telegram, data):
    put_txn(aws, "t1")

    app.lambda_handler(callback_event(data), None)

    assert "expense_category" not in get_txn(aws, "t1")
    assert telegram.answers == [{"id": "cb-1", "text": "Unknown category"}]


def test_callback_for_missing_transaction(aws, telegram):
    app.lambda_handler(callback_event("cat:gone:essential"), None)

    assert get_txn(aws, "gone") is None
    assert telegram.answers == [{"id": "cb-1", "text": "Transaction no longer exists"}]
    assert telegram.edits == []


def test_callback_for_pending_that_has_posted(aws, telegram):
    put_txn(aws, "posted-1", pending_transaction_id="pending-1")

    app.lambda_handler(callback_event("cat:pending-1:hobby"), None)

    assert get_txn(aws, "pending-1") is None
    assert get_txn(aws, "posted-1")["expense_category"] == "hobby"
    assert telegram.answers == [{"id": "cb-1", "text": "Saved: Hobby"}]


def test_telegram_errors_still_return_200(aws, telegram, monkeypatch):
    put_txn(aws, "t1")

    def fail(*args, **kwargs):
        raise TelegramError("Bad Request: message is not modified", 400)

    monkeypatch.setattr(telegram, "edit_message_text", fail)

    response = app.lambda_handler(callback_event("cat:t1:essential"), None)

    assert response["statusCode"] == 200
    assert get_txn(aws, "t1")["expense_category"] == "essential"


def test_non_callback_updates_are_ignored(aws, telegram):
    event = callback_event("cat:t1:essential")
    event["body"] = json.dumps({"update_id": 2, "message": {"text": "/start"}})

    response = app.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert telegram.answers == []
