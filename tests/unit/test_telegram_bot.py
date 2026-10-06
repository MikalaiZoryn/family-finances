import json
from pathlib import Path

from telegram_bot import app

EVENTS = Path(__file__).parents[2] / "events"


def test_telegram_update_returns_ok():
    event = json.loads((EVENTS / "telegram_update.json").read_text())

    response = app.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}
