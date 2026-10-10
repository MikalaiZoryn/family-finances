"""Connect real bank accounts with Plaid Hosted Link and manage linked Items.

Uses your local AWS credentials and the deployed stack's outputs. Works in whichever
environment the Plaid secret's `env` names (sandbox or production).

    python scripts/plaid_link.py set-credentials      # store client_id / secret / env
    python scripts/plaid_link.py link                 # connect a bank
    python scripts/plaid_link.py update <item_id>     # reconnect a broken login / add accounts
    python scripts/plaid_link.py list
    python scripts/plaid_link.py remove <item_id> [--purge-transactions]

Tokens and API secrets are never printed.
"""

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "shared"))

from shared.plaid import PlaidClient, PlaidError  # noqa: E402

CLIENT_USER_ID = "family"  # One household, one Plaid user.
CLIENT_NAME = "Family Finances"
PRODUCTS = ["transactions"]
DAYS_REQUESTED = 30  # Keep equal to INITIAL_SYNC_DAYS in template.yaml.


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Context:
    def __init__(self, stack_name: str, region: str):
        cfn = boto3.client("cloudformation", region_name=region)
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        self.outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        self.sm = boto3.client("secretsmanager", region_name=region)
        self.lambda_ = boto3.client("lambda", region_name=region)
        dynamodb = boto3.resource("dynamodb", region_name=region)
        self.items = dynamodb.Table(self.outputs["PlaidItemsTableName"])
        self.transactions = dynamodb.Table(self.outputs["TransactionsTableName"])

    def secret(self) -> dict[str, Any]:
        value = self.sm.get_secret_value(SecretId=self.outputs["PlaidSecretArn"])["SecretString"]
        return json.loads(value)

    def save_secret(self, secret: dict[str, Any]) -> None:
        self.sm.put_secret_value(
            SecretId=self.outputs["PlaidSecretArn"], SecretString=json.dumps(secret)
        )

    def plaid(self) -> tuple[PlaidClient, dict[str, Any]]:
        secret = self.secret()
        if secret.get("client_id", "REPLACE_ME") == "REPLACE_ME":
            sys.exit("No Plaid credentials yet: run `plaid_link.py set-credentials` first.")
        return PlaidClient(secret["client_id"], secret["secret"], secret["env"]), secret

    def access_token(self, item_id: str) -> str:
        token = self.secret().get("access_tokens", {}).get(item_id)
        if not token:
            sys.exit(f"No access token for item {item_id}. See `plaid_link.py list`.")
        return token

    def start_sync(self, item_id: str) -> None:
        self.lambda_.invoke(
            FunctionName=self.outputs["PlaidSyncFunctionName"],
            InvocationType="Event",
            Payload=json.dumps({"item_id": item_id}).encode(),
        )


def confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Hosted Link session
# ---------------------------------------------------------------------------


def run_hosted_link(plaid: PlaidClient, token: dict[str, Any]) -> dict[str, Any] | None:
    """Show the Hosted Link URL and wait until the user finishes. Returns the finished session."""
    print("Open this URL in your browser and follow the steps:\n")
    print(f"  {token['hosted_link_url']}\n")
    print("Plaid shows a confirmation when you're done.")
    while True:
        try:
            input("Press Enter once Link is finished (Ctrl+C to cancel)... ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return None
        session = finished_session(plaid.link_token_get(token["link_token"]))
        if session:
            return session
        print("Link isn't finished yet. Complete it in the browser, then press Enter again.")


def finished_session(link_token_get: dict[str, Any]) -> dict[str, Any] | None:
    """The latest finished Link session from /link/token/get, if any."""
    sessions = [s for s in link_token_get.get("link_sessions") or [] if s.get("finished_at")]
    return max(sessions, key=lambda s: s["finished_at"]) if sessions else None


def describe_exit(session: dict[str, Any]) -> str:
    exit_ = session.get("exit") or session.get("on_exit") or {}
    error = exit_.get("error") or {}
    if error:
        message = error.get("display_message") or error.get("error_message") or ""
        return f"{error.get('error_code')}: {message}".strip()
    status = (exit_.get("metadata") or {}).get("status")
    return f"exited without connecting{f' ({status})' if status else ''}"


def item_add_results(session: dict[str, Any]) -> list[dict[str, Any]]:
    return (session.get("results") or {}).get("item_add_results") or []


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_set_credentials(args, ctx: Context) -> None:
    secret = ctx.secret()
    tokens = secret.get("access_tokens", {})
    if tokens and secret.get("env") != args.env:
        print(
            f"The secret holds {len(tokens)} {secret.get('env')} access token(s); they do not "
            f"work in {args.env}. They and their PlaidItems rows will be dropped."
        )
        if not confirm("Continue?"):
            sys.exit("Aborted.")
        for item_id in tokens:
            ctx.items.delete_item(Key={"item_id": item_id})
        tokens = {}

    client_id = getpass.getpass(f"Plaid {args.env} client_id (input hidden): ").strip()
    plaid_secret = getpass.getpass(f"Plaid {args.env} secret (input hidden): ").strip()
    if not client_id or not plaid_secret:
        sys.exit("Both values are required.")
    secret.update(client_id=client_id, secret=plaid_secret, env=args.env, access_tokens=tokens)
    ctx.save_secret(secret)
    print(f"Saved {args.env} credentials. Lambdas pick them up on their next cold start.")


def cmd_link(args, ctx: Context) -> None:
    plaid, secret = ctx.plaid()
    token = plaid.link_token_create(
        CLIENT_USER_ID,
        CLIENT_NAME,
        webhook=ctx.outputs["PlaidWebhookUrl"],
        products=PRODUCTS,
        days_requested=DAYS_REQUESTED,
    )
    print(f"Plaid environment: {secret['env']}")
    session = run_hosted_link(plaid, token)
    if session is None:
        return
    results = item_add_results(session)
    if not results:
        sys.exit(
            f"No bank was connected: {describe_exit(session)} "
            f"(link_session_id {session.get('link_session_id')}). Run `link` again to retry."
        )
    for result in results:
        add_item(plaid, ctx, result, session.get("link_session_id"))


def add_item(plaid: PlaidClient, ctx: Context, result: dict[str, Any], session_id: str) -> None:
    institution = result.get("institution") or {}
    institution_id = institution.get("institution_id")
    institution_name = institution.get("name") or institution_id
    existing = existing_items(ctx, institution_id)

    # Exchange right away: the public token is single-use and expires within minutes.
    exchange = plaid.item_public_token_exchange(result["public_token"])
    item_id, access_token = exchange["item_id"], exchange["access_token"]

    if existing and not confirm(
        f"{institution_name} is already linked as item {', '.join(existing)}. "
        "Keep this second connection (billed separately)?"
    ):
        plaid.item_remove(access_token)
        print("Removed the duplicate connection. To fix the existing one, run "
              f"`plaid_link.py update {existing[0]}`.")
        return

    # Persist the token before anything else can fail.
    secret = ctx.secret()
    secret.setdefault("access_tokens", {})[item_id] = access_token
    ctx.save_secret(secret)

    accounts = [
        {k: a[k] for k in ("id", "name", "mask", "type", "subtype") if a.get(k) is not None}
        for a in result.get("accounts") or []
    ]
    # update_item, not put_item: the first sync may already have written the cursor.
    ctx.items.update_item(
        Key={"item_id": item_id},
        UpdateExpression=(
            "SET institution_id = :iid, institution_name = :iname, accounts = :acc, "
            "link_session_id = :sid, created_at = if_not_exists(created_at, :now), "
            "#status = :ok"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":iid": institution_id,
            ":iname": institution_name,
            ":acc": accounts,
            ":sid": session_id,
            ":now": now(),
            ":ok": "ok",
        },
    )
    ctx.start_sync(item_id)
    print(f"\nLinked item {item_id} at {institution_name}: {len(accounts)} account(s)")
    for a in accounts:
        print(f"  - {account_line(a)}")
    print("First sync requested; Plaid sends the rest of the history over the next minutes.")


def account_line(account: dict[str, Any]) -> str:
    kind = account.get("subtype") or account.get("type")
    return f"{account.get('name')} ••{account.get('mask', '')} ({kind})"


def existing_items(ctx: Context, institution_id: str | None) -> list[str]:
    if not institution_id:
        return []
    rows = ctx.items.scan(FilterExpression=Attr("institution_id").eq(institution_id))["Items"]
    return [r["item_id"] for r in rows if r.get("status") != "revoked"]


def cmd_update(args, ctx: Context) -> None:
    plaid, _ = ctx.plaid()
    access_token = ctx.access_token(args.item_id)
    token = plaid.link_token_create(
        CLIENT_USER_ID,
        CLIENT_NAME,
        webhook=ctx.outputs["PlaidWebhookUrl"],
        access_token=access_token,
    )
    session = run_hosted_link(plaid, token)
    if session is None:
        return
    exit_ = session.get("exit") or session.get("on_exit")
    if exit_ and not item_add_results(session) and not session.get("on_success"):
        sys.exit(
            f"Reconnect not completed: {describe_exit(session)} "
            f"(link_session_id {session.get('link_session_id')}). Run `update` again to retry."
        )

    # Update mode yields no new public token: the existing access token keeps working.
    item = plaid.item_get(access_token)["item"]
    error = item.get("error")
    if error:
        sys.exit(f"The bank still reports {error.get('error_code')}. Run `update` again.")
    ctx.items.update_item(
        Key={"item_id": args.item_id},
        UpdateExpression="SET #status = :ok, status_reason = :r, status_updated_at = :now",
        ConditionExpression="attribute_exists(item_id)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":ok": "ok", ":r": "UPDATE_MODE", ":now": now()},
    )
    ctx.start_sync(args.item_id)
    print(f"Reconnected item {args.item_id}. Sync requested.")


def cmd_list(args, ctx: Context) -> None:
    secret = ctx.secret()
    rows = ctx.items.scan()["Items"]
    print(f"Plaid environment: {secret.get('env')}")
    if not rows:
        print("No linked items. Run `plaid_link.py link`.")
        return
    for r in sorted(rows, key=lambda r: r.get("created_at", "")):
        has_token = r["item_id"] in secret.get("access_tokens", {})
        print(
            f"{r['item_id']}  {r.get('institution_name', r.get('institution_id', '?'))}  "
            f"status={r.get('status', 'ok')}"
            f"{' (' + r['status_reason'] + ')' if r.get('status_reason') else ''}  "
            f"accounts={len(r.get('accounts', []))}  "
            f"last_synced={r.get('last_synced_at', 'never')}"
            f"{'' if has_token else '  [no access token]'}"
        )
        for a in r.get("accounts", []):
            print(f"    - {account_line(a)}")


def cmd_remove(args, ctx: Context) -> None:
    plaid, secret = ctx.plaid()
    token = secret.get("access_tokens", {}).get(args.item_id)
    what = "and its transactions " if args.purge_transactions else ""
    if not args.yes and not confirm(f"Disconnect item {args.item_id} {what}from Plaid?"):
        sys.exit("Aborted.")
    if token:
        try:
            plaid.item_remove(token)
        except PlaidError as e:
            if e.error_code != "ITEM_NOT_FOUND":
                raise
    secret = ctx.secret()
    secret.get("access_tokens", {}).pop(args.item_id, None)
    ctx.save_secret(secret)
    ctx.items.delete_item(Key={"item_id": args.item_id})
    deleted = purge_transactions(ctx, args.item_id) if args.purge_transactions else 0
    print(f"Removed item {args.item_id}" + (f" and {deleted} transaction(s)." if deleted else "."))


def purge_transactions(ctx: Context, item_id: str) -> int:
    deleted = 0
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("item_id").eq(item_id),
        "ProjectionExpression": "transaction_id",
    }
    with ctx.transactions.batch_writer() as batch:
        while True:
            page = ctx.transactions.scan(**kwargs)
            for row in page["Items"]:
                batch.delete_item(Key={"transaction_id": row["transaction_id"]})
                deleted += 1
            if "LastEvaluatedKey" not in page:
                return deleted
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack-name", default="family-finances")
    parser.add_argument("--region", default="us-east-1")
    sub = parser.add_subparsers(dest="command", required=True)

    creds = sub.add_parser("set-credentials", help="Store Plaid API keys in Secrets Manager")
    creds.add_argument("--env", choices=["production", "sandbox"], default="production")
    creds.set_defaults(func=cmd_set_credentials)

    sub.add_parser("link", help="Connect a bank with Hosted Link").set_defaults(func=cmd_link)

    update = sub.add_parser("update", help="Reconnect an Item or change its shared accounts")
    update.add_argument("item_id")
    update.set_defaults(func=cmd_update)

    sub.add_parser("list", help="Show linked Items").set_defaults(func=cmd_list)

    remove = sub.add_parser("remove", help="Disconnect an Item (/item/remove)")
    remove.add_argument("item_id")
    remove.add_argument("--purge-transactions", action="store_true")
    remove.add_argument("--yes", action="store_true", help="Skip the confirmation")
    remove.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    try:
        args.func(args, Context(args.stack_name, args.region))
    except PlaidError as e:
        # str(e) carries error type/code/message and request_id - never tokens.
        sys.exit(f"Plaid error {e}")


if __name__ == "__main__":
    main()
