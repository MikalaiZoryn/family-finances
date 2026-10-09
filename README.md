# Family Finances v1

Serverless service that syncs family transactions from Plaid into DynamoDB and
asks for their categories through a private Telegram bot. See
[Requirements.txt](Requirements.txt) for the full requirements.

> Status: Plaid sync Lambda implemented (sandbox). Telegram Lambda is a stub.
> Plaid webhook signature verification is not implemented yet.

## Layout

```
template.yaml              SAM application (API, Lambdas, DynamoDB, secrets)
samconfig.toml             SAM CLI defaults (stack family-finances, us-east-1)
infra/github-oidc.yaml     One-time bootstrap: GitHub OIDC, deploy roles, artifacts bucket
src/plaid_sync/            Plaid webhook Lambda
src/telegram_bot/          Telegram webhook Lambda
src/shared/                Lambda layer with shared code (import as `shared`)
events/                    Sample API Gateway events for `sam local invoke`
scripts/plaid_sandbox.py   Link a Plaid sandbox Item, fire webhooks, refresh transactions
tests/                     Unit tests
.github/workflows/         CI (PRs / branches) and Deploy (main)
```

## Data model

`TransactionsTable` — partition key `transaction_id` (Plaid transaction ID).

| Attribute             | Notes                                                                |
|-----------------------|----------------------------------------------------------------------|
| `transaction_date`    | Plaid `date`, ISO `YYYY-MM-DD`                                       |
| `status`              | `pending` or `posted`                                                |
| `amount`, `currency`  | Plaid amount (positive = money out), ISO currency code               |
| `item_id`, `account_id`, `account_name`, `account_mask` | Plaid Item / account               |
| `name`, `merchant_name`, `authorized_date`, `pending_transaction_id`, `payment_channel` | Plaid fields |
| `plaid_category_primary`, `plaid_category_detailed` | Plaid `personal_finance_category`      |
| `created_at`, `updated_at` | ISO UTC timestamps                                              |
| `expense_category`    | **Absent** until the user categorizes it (never stored as NULL)      |
| `telegram_message_id` | Set when the transaction was sent to Telegram, prevents re-sending   |

Plaid fields that are null are omitted. The sync only `SET`s Plaid-owned
attributes, so `expense_category` and `telegram_message_id` survive updates.
Transactions Plaid reports as removed are deleted.

GSI `CategoryDateIndex` (`expense_category`, `transaction_date`) is sparse and
contains only categorized transactions. Monthly totals = one query per category
with `transaction_date BETWEEN 'YYYY-MM-01' AND 'YYYY-MM-31'`.

`PlaidItemsTable` — partition key `item_id`, one row per Plaid Item.

| Attribute          | Notes                                                                  |
|--------------------|------------------------------------------------------------------------|
| `cursor`           | Plaid `/transactions/sync` cursor; absent until the first sync         |
| `last_synced_at`   | ISO UTC timestamp of the last successful sync                          |
| `last_sync_counts` | added / modified / removed / upserted / skipped counts of the last sync |
| `sync_start_date`  | Transactions dated before this are never stored                        |
| `institution_id`, `created_at` | Set when the Item is linked                                |

Access tokens are **not** stored here; they live in the Plaid secret under
`access_tokens.<item_id>`.

## Plaid sync

A `TRANSACTIONS` webhook with code `SYNC_UPDATES_AVAILABLE`, `INITIAL_UPDATE`,
`HISTORICAL_UPDATE` or `DEFAULT_UPDATE` (or a direct invoke with
`{"item_id": "..."}`) makes the Lambda page through `/transactions/sync` from the
stored cursor, so only new, modified and removed transactions are processed.
The new cursor is saved only after all pages are applied, so a failed run
is retried from the previous cursor. All writes are idempotent.

On the first sync of an Item (no row or no cursor), only the last
`INITIAL_SYNC_DAYS` (default 30) of history is requested, and `sync_start_date`
is recorded. Older transactions are skipped on every later sync too.

## Local development

```bash
python -m venv .venv
. .venv/Scripts/activate        # Windows (Git Bash); use .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
ruff check .
pytest
```

With the [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) and Docker:

```bash
sam validate --lint
sam build
sam local invoke PlaidSyncFunction -e events/plaid_webhook.json
sam local invoke TelegramBotFunction -e events/telegram_update.json
```

## First-time AWS setup

1. **Bootstrap CI/CD** (once per account, with admin credentials):

   ```bash
   aws cloudformation deploy \
     --region us-east-1 \
     --stack-name family-finances-bootstrap \
     --template-file infra/github-oidc.yaml \
     --capabilities CAPABILITY_IAM
   # add  --parameter-overrides CreateOidcProvider=false  if the account already
   # has the token.actions.githubusercontent.com OIDC provider

   aws cloudformation describe-stacks --region us-east-1 \
     --stack-name family-finances-bootstrap --query "Stacks[0].Outputs"
   ```

2. **Configure GitHub** (Settings → Environments → create `production`;
   Settings → Secrets and variables → Actions → *Variables*):

   | Variable                    | Value                                          |
   |-----------------------------|------------------------------------------------|
   | `AWS_DEPLOY_ROLE_ARN`       | bootstrap output `GitHubDeployRoleArn`         |
   | `CFN_EXECUTION_ROLE_ARN`    | bootstrap output `CloudFormationExecutionRoleArn` |
   | `SAM_ARTIFACTS_BUCKET`      | bootstrap output `SamArtifactsBucketName`      |
   | `ALLOWED_TELEGRAM_USER_IDS` | comma-separated numeric Telegram user IDs      |

   No AWS secrets are stored in GitHub — the workflow uses OIDC.

3. **Deploy**: push to `main` (or run the *Deploy* workflow manually).

4. **Fill in secrets** (Secrets Manager, created with placeholder values):
   - `family-finances/plaid` — set `client_id`, `secret`, `env`.
   - `family-finances/telegram` — set `bot_token`. `webhook_secret_token` is
     generated automatically; pass it as `secret_token` when calling Telegram
     `setWebhook` with the `TelegramWebhookUrl` stack output.

   Secrets, tables and their data are retained if the stack is deleted.

## Plaid sandbox

1. Create a Plaid account and copy the **sandbox** `client_id` and `secret` from
   the [Plaid dashboard](https://dashboard.plaid.com/developers/keys).
2. Put them into the `family-finances/plaid` secret, keeping `env` = `sandbox`:

   ```bash
   aws secretsmanager put-secret-value --secret-id family-finances/plaid \
     --secret-string '{"client_id":"...","secret":"...","env":"sandbox","access_tokens":{}}'
   ```

3. Link a sandbox Item (First Platypus Bank, `user_transactions_dynamic`). The
   script stores the access token in the secret, creates the PlaidItemsTable
   row and registers the stack's `PlaidWebhookUrl` as the Item's webhook:

   ```bash
   python scripts/plaid_sandbox.py link
   ```

   Plaid then sends `INITIAL_UPDATE` / `HISTORICAL_UPDATE`, which trigger the
   first sync. `SYNC_UPDATES_AVAILABLE` only starts arriving after that first sync.

4. Generate more activity and trigger a sync:

   ```bash
   python scripts/plaid_sandbox.py refresh <item_id>       # new / pending→posted transactions
   python scripts/plaid_sandbox.py fire-webhook <item_id>  # force a sync webhook
   aws lambda invoke --function-name <PlaidSyncFunction name> \
     --cli-binary-format raw-in-base64-out --payload '{"item_id":"<item_id>"}' out.json
   ```
