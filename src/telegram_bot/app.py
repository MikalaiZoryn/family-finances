"""Telegram webhook handler - processes bot commands and category callbacks.

Stub: acknowledges the update and does nothing else yet.
"""

import logging

from shared.http import json_response

logger = logging.getLogger()


def lambda_handler(event, context):
    # Never log the request body: it may contain financial data.
    logger.info(
        "Telegram update received",
        extra={
            "path": event.get("rawPath"),
            "request_id": event.get("requestContext", {}).get("requestId"),
        },
    )
    return json_response(200, {"ok": True})
