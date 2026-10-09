"""Plaid sandbox helper: link a test Item and trigger sync webhooks.

Uses your local AWS credentials and the deployed stack's outputs.

    python scripts/plaid_sandbox.py link
    python scripts/plaid_sandbox.py fire-webhook <item_id>
    python scripts/plaid_sandbox.py refresh <item_id>
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "shared"))

from shared.plaid import PlaidClient  # noqa: E402

INITIAL_SYNC_DAYS = 30


def stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def load_secret(sm, secret_arn: str) -> dict:
    return json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])


def client_for(secret: dict) -> PlaidClient:
    if secret.get("client_id", "REPLACE_ME") == "REPLACE_ME":
        sys.exit("Set client_id and secret in the Plaid secret first (see README).")
    if secret.get("env", "sandbox") != "sandbox":
        sys.exit("This script only works with env=sandbox.")
    return PlaidClient(secret["client_id"], secret["secret"], "sandbox")


def access_token_for(secret: dict, item_id: str) -> str:
    token = secret.get("access_tokens", {}).get(item_id)
    if not token:
        sys.exit(f"No access token for item {item_id} in the Plaid secret.")
    return token


def cmd_link(args, outputs, sm) -> None:
    secret = load_secret(sm, outputs["PlaidSecretArn"])
    plaid = client_for(secret)

    public_token = plaid.sandbox_public_token_create(
        institution_id=args.institution,
        products=["transactions"],
        webhook=outputs["PlaidWebhookUrl"],
        override_username=args.user,
        days_requested=INITIAL_SYNC_DAYS,
    )["public_token"]
    exchange = plaid.item_public_token_exchange(public_token)
    item_id, access_token = exchange["item_id"], exchange["access_token"]

    secret.setdefault("access_tokens", {})[item_id] = access_token
    sm.put_secret_value(SecretId=outputs["PlaidSecretArn"], SecretString=json.dumps(secret))

    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    dynamodb.Table(outputs["PlaidItemsTableName"]).put_item(
        Item={
            "item_id": item_id,
            "institution_id": args.institution,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    print(f"Linked sandbox item: {item_id}")


def cmd_fire_webhook(args, outputs, sm) -> None:
    secret = load_secret(sm, outputs["PlaidSecretArn"])
    client_for(secret).sandbox_item_fire_webhook(access_token_for(secret, args.item_id))
    print("Fired SYNC_UPDATES_AVAILABLE webhook.")


def cmd_refresh(args, outputs, sm) -> None:
    secret = load_secret(sm, outputs["PlaidSecretArn"])
    client_for(secret).transactions_refresh(access_token_for(secret, args.item_id))
    print("Requested /transactions/refresh.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack-name", default="family-finances")
    parser.add_argument("--region", default="us-east-1")
    sub = parser.add_subparsers(dest="command", required=True)

    link = sub.add_parser("link", help="Create a sandbox Item and store its access token")
    link.add_argument("--institution", default="ins_109508", help="Default: First Platypus Bank")
    link.add_argument(
        "--user",
        default="user_transactions_dynamic",
        help="Sandbox username; user_transactions_dynamic produces new transactions on refresh",
    )
    link.set_defaults(func=cmd_link)

    fire = sub.add_parser("fire-webhook", help="Fire SYNC_UPDATES_AVAILABLE for an Item")
    fire.add_argument("item_id")
    fire.set_defaults(func=cmd_fire_webhook)

    refresh = sub.add_parser("refresh", help="Generate new sandbox transactions for an Item")
    refresh.add_argument("item_id")
    refresh.set_defaults(func=cmd_refresh)

    args = parser.parse_args()
    outputs = stack_outputs(args.stack_name, args.region)
    sm = boto3.client("secretsmanager", region_name=args.region)
    args.func(args, outputs, sm)


if __name__ == "__main__":
    main()
