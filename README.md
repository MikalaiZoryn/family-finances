# Family Finances

Serverless service that syncs family transactions from Plaid into DynamoDB and
asks for their categories through a private Telegram bot. See
[Requirements.txt](Requirements.txt) for the full requirements.

> Status: infrastructure scaffold. Both Lambdas are stubs that return `200 {"ok": true}`.

## Layout

```
template.yaml              SAM application (API, Lambdas, DynamoDB, secrets)
samconfig.toml             SAM CLI defaults (stack family-finances, us-east-1)
infra/github-oidc.yaml     One-time bootstrap: GitHub OIDC, deploy roles, artifacts bucket
src/plaid_sync/            Plaid webhook Lambda
src/telegram_bot/          Telegram webhook Lambda
src/shared/                Lambda layer with shared code (import as `shared`)
events/                    Sample API Gateway events for `sam local invoke`
tests/                     Unit tests
.github/workflows/         CI (PRs / branches) and Deploy (main)
```

## Data model

`TransactionsTable` — partition key `transaction_id` (Plaid transaction ID).

| Attribute             | Notes                                                                |
|-----------------------|----------------------------------------------------------------------|
| `transaction_date`    | Plaid `date`, ISO `YYYY-MM-DD`                                       |
| `expense_category`    | **Absent** until the user categorizes it (never stored as NULL)      |
| `telegram_message_id` | Set when the transaction was sent to Telegram, prevents re-sending   |

GSI `CategoryDateIndex` (`expense_category`, `transaction_date`) is sparse and
contains only categorized transactions. Monthly totals = one query per category
with `transaction_date BETWEEN 'YYYY-MM-01' AND 'YYYY-MM-31'`.

`PlaidItemsTable` — partition key `item_id`; stores the sync cursor per Plaid Item.

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
