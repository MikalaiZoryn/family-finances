import json
from typing import Any


def json_response(status_code: int, body: Any) -> dict:
    """Build an API Gateway HTTP API (payload v2) response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
