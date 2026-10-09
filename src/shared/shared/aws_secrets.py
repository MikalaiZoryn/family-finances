"""Secrets Manager access with a per-container cache."""

import json
from typing import Any

import boto3

_cache: dict[str, dict[str, Any]] = {}


def get_secret_json(secret_id: str, refresh: bool = False) -> dict[str, Any]:
    """Return the secret's JSON value, cached for the lifetime of the container."""
    if refresh or secret_id not in _cache:
        client = boto3.client("secretsmanager")
        value = client.get_secret_value(SecretId=secret_id)["SecretString"]
        _cache[secret_id] = json.loads(value)
    return _cache[secret_id]


def clear_cache() -> None:
    _cache.clear()
