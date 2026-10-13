# Family Finances v1

Serverless service that syncs family transactions from Plaid (and Apple Card CSV
exports posted to the family chat) into DynamoDB and asks for their categories through a private Telegram bot. See
[Requirements.txt](Requirements.txt) for the full requirements.

> Status: Plaid sync Lambda implemented; real banks are linked with Hosted Link
> from `scripts/plaid_link.py`. Plaid webhooks are signature-verified, and each
> sync sends its new expenses to Telegram right away. Telegram Lambda sweeps up
> any expenses still unsent and saves the chosen category. Apple Card CSV
> exports posted to the family chat are imported (and re-imported without
> duplicates) by the Apple Card import Lambda. Aggregation
> Lambda totals categorized expenses against monthly budgets and posts a report
> daily at 22:30 America/Los_Angeles; the Telegram sweep runs daily at 22:00 as a
> reconciliation. Plaid integration state and
> remaining setup tasks: [PLAID_PLAN.md](PLAID_PLAN.md).

## Layout

```
template.yaml              SAM application (API, Lambdas, DynamoDB, secrets)
samconfig.toml             SAM CLI defaults (stack family-finances, us-east-1)
infra/github-oidc.yaml     One-time bootstrap: GitHub OIDC, deploy roles, artifacts bucket
src/plaid_sync/            Plaid webhook Lambda
src/telegram_bot/          Telegram webhook Lambda
src/apple_card_import/     Apple Card CSV import Lambda (started by the Telegram bot)
src/aggregation/           Monthly budget aggregation Lambda
src/shared/                Lambda layer with shared code (import as `shared`)
events/                    Sample API Gateway events for `sam local invoke`
scripts/plaid_link.py      Connect real banks (Hosted Link), reconnect, list, remove Items
scripts/plaid_sandbox.py   Link a Plaid sandbox Item, fire webhooks, refresh transactions
tests/                     Unit tests
.github/workflows/         CI (PRs / branches) and Deploy (main)
```

## Data model

`TransactionsTable` — partition key `transaction_id` (Plaid transaction ID, or
`apple-<hash>` for Apple Card rows).

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
| `telegram_message_id`, `telegram_chat_id`, `telegram_sent_at` | Set when sent to Telegram; prevents re-sending |
| `categorized_at`, `categorized_by` | When and by which Telegram user ID the category was chosen |
| `source`              | `apple_card` for Apple Card rows; absent for Plaid rows              |
| `clearing_date`, `apple_category`, `apple_type`, `purchased_by` | Apple Card CSV fields |
| `import_file_name`, `imported_by` | File and Telegram user ID that first imported the Apple Card row |

Plaid fields that are null are omitted. The sync only `SET`s Plaid-owned
attributes, so `expense_category` and `telegram_*` fields survive updates.
Transactions Plaid reports as removed are deleted.

When a pending transaction posts, Plaid gives it a new `transaction_id` (with
`pending_transaction_id` pointing at the old one) and removes the pending one.
The sync copies `expense_category`, `categorized_*` and `telegram_*` from the
pending row to the posted row (without overwriting), so a category chosen while
pending is kept and the transaction is not sent again.

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
| `institution_id`, `institution_name`, `accounts`, `link_session_id`, `created_at` | Set when the Item is linked |
| `status`, `status_reason`, `status_updated_at` | `ok`, `needs_update`, `pending_disconnect`, `new_accounts` or `revoked`; absent = ok |

Access tokens are **not** stored here; they live in the Plaid secret under
`access_tokens.<item_id>`.

`CategoryLimitsTable` — partition key `category` (category key, e.g. `essential`).

| Attribute | Notes                                                              |
|-----------|--------------------------------------------------------------------|
| `limit`   | Monthly limit; `-1` = no limit. A category without a row has no limit |

`CategoryStateTable` — partition key `category`, sort key `month` (`YYYY-MM`).

| Attribute          | Notes                                                           |
|--------------------|-----------------------------------------------------------------|
| `limit`            | Limit in force when the month's row was created                 |
| `budget`           | Budget for the month; `-1` = no limit. May be negative after overspending |
| `current_expenses` | Sum of categorized transaction amounts dated in the month       |
| `updated_at`       | ISO UTC timestamp of the last aggregation                        |

## Plaid sync

Every webhook's `Plaid-Verification` JWT is checked (ES256, key fetched by `kid`
and cached, at most 5 minutes old, SHA-256 of the raw body must match); anything
else gets a 401. Verified webhooks are acknowledged at once and the sync runs in
an async invoke of the same function.

A `TRANSACTIONS` webhook with code `SYNC_UPDATES_AVAILABLE`, `INITIAL_UPDATE`,
`HISTORICAL_UPDATE` or `DEFAULT_UPDATE` (or a direct invoke with
`{"item_id": "..."}`) makes the Lambda page through `/transactions/sync` from the
stored cursor, so only new, modified and removed transactions are processed.
The new cursor is saved only after all pages are applied, so a failed run
is retried from the previous cursor. All writes are idempotent.

**New expenses to Telegram** — once the cursor is saved, the sync sends the
transactions it just *added* to `TELEGRAM_CHAT_ID`, using the same filter and
message as the Telegram bot (see *Sending* below). Oldest first, at most
`MAX_MESSAGES_PER_SYNC` (20) per sync. It skips modified transactions and
posted transactions whose pending version was already sent or categorized.
Only the run that advanced the cursor sends, so duplicate webhooks for one
Item can't send twice. Telegram errors are logged and don't fail the sync.
Anything not sent waits for the Telegram bot's sweep.

On the first sync of an Item (no row or no cursor), only the last
`INITIAL_SYNC_DAYS` (default 30) of history is requested, and `sync_start_date`
is recorded. Older transactions are skipped on every later sync too.

**Item health** — `ITEM` webhooks (`ERROR`, `PENDING_DISCONNECT`,
`PENDING_EXPIRATION`, `USER_PERMISSION_REVOKED`, `LOGIN_REPAIRED`,
`NEW_ACCOUNTS_AVAILABLE`) and sync errors that need the user (e.g.
`ITEM_LOGIN_REQUIRED`) set the Item's `status` and post one Telegram alert per
change with the command to run (`plaid_link.py update <item_id>`). Transient
errors (e.g. `INSTITUTION_DOWN`) fail the async invoke so Lambda retries it.

## Apple Card import

Apple Card isn't available through Plaid, so its transactions come from the
CSV export (Wallet → Apple Card → Card Balance → Export Transactions). Anyone in
`ALLOWED_TELEGRAM_USER_IDS` posts the file to the `TELEGRAM_CHAT_ID` chat; the
Telegram Lambda checks the chat, the sender, the `.csv` name and the size
(`MAX_CSV_BYTES`, 1 MB) and starts the import as an async invoke. Other files and
chats are ignored silently.

The import Lambda downloads the file, stores the rows and replies to the message
with a summary (`5 new, 2 updated, 40 unchanged · 4 sent for categorization`).
New expenses are sent for categorization like Plaid's (at most
`MAX_MESSAGES_PER_IMPORT`, 20); Apple `Payment` rows are stored but never sent.
A file without the expected columns gets an error reply. Failed imports aren't
retried (they already replied); post the file again.

**Re-imports** — exports overlap, and Apple has no transaction ID, so file rows
are matched to stored Apple Card rows dated within the file's range:

1. Same date, description, amount and `Purchased By` → the same transaction.
2. Otherwise same date, description and buyer with a different amount (a tip
   added when the charge cleared, a gas-station hold) → the same transaction,
   updated in place.
3. Otherwise a new row, ID `apple-` + 24 hex of a SHA-256 of those fields and an
   occurrence counter (two identical coffees on one day stay two rows).

Matched rows get only their CSV fields updated (clearing date, status, amount, …);
the category and Telegram fields are kept. Stored rows that are missing from a
later file are kept, never deleted.

## Telegram categorization

Categories are defined in `src/telegram_bot/app.py` (`CATEGORIES`): `essential`
(Essential), `weekend_fun` (Weekend Fun), `hobby` (Hobby), `sport` (Sport),
`subscription` (Subscription), `investment` (Investment), `vacation`
(Vacation), `miscellaneous`
(Miscellaneous). The key is stored in `expense_category`; the label is shown on
the buttons, two per row (`BUTTONS_PER_ROW`).

**Sending** — new expenses are normally sent by the Plaid sync and the Apple
Card import (above). Any
non-HTTP invoke of the Telegram Lambda is a fallback sweep, run daily at 22:00
`TimeZone` (default `America/Los_Angeles`) by the EventBridge schedule
`family-finances-telegram-reconciliation`: it scans for
transactions without `expense_category` and `telegram_message_id`, keeps only
expenses (`amount > 0`, pending or posted) and skips transfers
(`TRANSFER_IN` / `TRANSFER_OUT`), income/payroll (`INCOME`) and credit card
payments (`LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`, Apple `Payment`). Oldest first, at most
`MAX_MESSAGES_PER_RUN` (20) per run go to `TELEGRAM_CHAT_ID`, one message per
transaction with a button per category.

```bash
aws lambda invoke --function-name <TelegramBotFunction name>   --cli-binary-format raw-in-base64-out --payload '{}' out.json
```

**Button press** — Telegram calls the webhook; the Lambda checks the
`X-Telegram-Bot-Api-Secret-Token` header and that the user is in
`ALLOWED_TELEGRAM_USER_IDS`, saves the category, and edits the message to show
it (buttons removed). A press on a pending transaction that has since posted is
saved on the posted transaction.

## Budget aggregation

Categories are shared via `src/shared/shared/categories.py`. Limits are set
manually in `CategoryLimitsTable` (no bot commands yet):

```bash
aws dynamodb put-item --table-name <CategoryLimitsTableName> \
  --item '{"category":{"S":"essential"},"limit":{"N":"1000"}}'
```

The EventBridge schedule `family-finances-aggregation` runs it daily at 22:30
`TimeZone` (default `America/Los_Angeles`). Each run (`{}` = current month in
`TimeZone`, so the run on the last day still reports the closing month; or
`{"month": "YYYY-MM"}`) does this for every category:

1. Recompute the previous month's `current_expenses`, so transactions from last
   month that were categorized late still count.
2. Write the month's row with `budget` and `current_expenses`.
3. Post a report to `TELEGRAM_CHAT_ID`.

Expenses are summed from `CategoryDateIndex`.

Budget rules (`P` = previous month's row):

- limit `-1` → budget `-1` (no limit)
- limit set, and either no `P` or `P` had no limit → budget = limit
- limit set and `P` had a limit → budget = `P.budget − P.current_expenses + limit`
  (overspending carries over and can make the budget negative)

The limit is captured when a month's row is first created, so a limit change
applies from the next month. If a month was never aggregated, the following
month starts fresh at the limit. Runs are idempotent.

```bash
aws lambda invoke --function-name <AggregationFunction name> \
  --cli-binary-format raw-in-base64-out --payload '{}' out.json
```

## Local development

```bash
python -m venv .venvtelg
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
sam local invoke TelegramBotFunction -e events/telegram_send.json
sam local invoke TelegramBotFunction -e events/telegram_document.json
sam local invoke AppleCardImportFunction -e events/apple_card_import.json
sam local invoke AggregationFunction -e events/aggregation_run.json
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
   | `TELEGRAM_CHAT_ID`          | chat that receives transactions (see below)    |

   No AWS secrets are stored in GitHub — the workflow uses OIDC.

3. **Deploy**: push to `main` (or run the *Deploy* workflow manually).

4. **Fill in secrets** (Secrets Manager, created with placeholder values):
   - `family-finances/plaid` — set `client_id`, `secret`, `env`.
   - `family-finances/telegram` — set `bot_token`. `webhook_secret_token` is
     generated automatically; pass it as `secret_token` when calling Telegram
     `setWebhook` with the `TelegramWebhookUrl` stack output.

5. **Find the Telegram chat ID**: send any message to the bot (or add it to a
   family group and post there) *before* registering the webhook, then open
   `https://api.telegram.org/bot<bot_token>/getUpdates` and copy
   `message.chat.id` (negative for groups). For a private chat it equals your
   user ID. Set it as the `TELEGRAM_CHAT_ID` variable and redeploy.

6. **Let the bot see CSV files in a group** (Apple Card import): bots in groups
   only receive commands and replies unless privacy mode is off. In BotFather run
   `/setprivacy` → your bot → *Disable*, then remove the bot from the group and
   add it again (or make it a group admin). If `setWebhook` was called with
   `allowed_updates`, make sure the list includes `message`.

   Secrets, tables and their data are retained if the stack is deleted.

## Connecting real banks (production)

1. Get production access in the Plaid Dashboard, then store the production keys
   (prompts with hidden input; drops tokens from the other environment):

   ```bash
   python scripts/plaid_link.py set-credentials            # --env sandbox to go back
   ```

2. Connect each bank — prints a Plaid Hosted Link URL; finish in the browser,
   then press Enter. The access token goes to the Plaid secret, the Item to
   PlaidItemsTable, and the first sync starts:

   ```bash
   python scripts/plaid_link.py link
   python scripts/plaid_link.py list
   ```

3. When Telegram says a bank needs attention:

   ```bash
   python scripts/plaid_link.py update <item_id>    # reconnect / share new accounts
   python scripts/plaid_link.py remove <item_id> [--purge-transactions]
   ```

`link` also works in sandbox (First Platypus Bank, `user_good` / `pass_good`).

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
