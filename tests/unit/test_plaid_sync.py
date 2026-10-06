import json
from pathlib import Path

from plaid_sync import app

EVENTS = Path(__file__).parents[2] / "events"


def test_plaid_webhook_returns_ok():
    event = json.loads((EVENTS / "plaid_webhook.json").read_text())

    response = app.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}
