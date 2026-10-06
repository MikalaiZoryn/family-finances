"""Plaid webhook handler - synchronizes transactions via Plaid Transactions Sync API.

Stub: acknowledges the webhook and does nothing else yet.
"""

import logging

from shared.http import json_response

logger = logging.getLogger()


def lambda_handler(event, context):
    # Never log the request body: it may contain financial data.
    logger.info(
        "Plaid webhook received",
        extra={
            "path": event.get("rawPath"),
            "request_id": event.get("requestContext", {}).get("requestId"),
        },
    )
    return json_response(200, {"ok": True})
