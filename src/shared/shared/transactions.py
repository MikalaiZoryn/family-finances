"""TransactionsTable writes shared by the Plaid sync and the Apple Card import."""

from datetime import UTC, datetime
from typing import Any


def upsert_transaction(
    table, item: dict[str, Any], carry_over: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Update only source-owned fields, preserving user/bot fields like expense_category.

    carry_over attributes are set only if the item does not already have them.
    Returns the stored row.
    """
    names = {"#updated_at": "updated_at", "#created_at": "created_at"}
    values: dict[str, Any] = {":now": datetime.now(UTC).isoformat(timespec="seconds")}
    sets = ["#updated_at = :now", "#created_at = if_not_exists(#created_at, :now)"]
    for i, (attr, value) in enumerate(item.items()):
        if attr == "transaction_id":
            continue
        names[f"#a{i}"] = attr
        values[f":v{i}"] = value
        sets.append(f"#a{i} = :v{i}")
    for i, (attr, value) in enumerate((carry_over or {}).items()):
        names[f"#c{i}"] = attr
        values[f":c{i}"] = value
        sets.append(f"#c{i} = if_not_exists(#c{i}, :c{i})")
    return table.update_item(
        Key={"transaction_id": item["transaction_id"]},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )["Attributes"]
