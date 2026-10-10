# PLAID_PLAN.md

## Goal

Set up a production Plaid Link flow to connect our real family bank and
credit-card accounts to the existing family-finances service (Plaid
Transactions Sync into DynamoDB, categorized via a private Telegram bot).
Previously only a sandbox script linked Items. (workflow_id: wf_616e875531fdfc0a)

## Scope

- Products: `transactions` only, in Link's `products` array — it feeds the
  existing sync, Telegram categorization and budget aggregation. Covers
  checking, savings and credit cards (SELECT-001, CONV-001, TXN-021).
- Not included: `investments` / `liabilities` in `additional_consented_products`
  (SELECT-005 default for a PFM). Declined by the developer: the app has no
  net-worth or loan features; adding them later needs update-mode re-consent
  (CONV-011). Recurring Transactions add-on is the add-later candidate for
  subscription tracking (TXN-016) — it is enabled in production.
- No money movement, no lending decision, no account/routing numbers, no
  identity verification. US institutions only.
- Platform: Python 3.13 on AWS Lambda (SAM, HTTP API, DynamoDB). No web
  frontend, so Link runs as **Hosted Link** (HOSTED-001..005) launched from a
  local CLI (`scripts/plaid_link.py`).
- History: `transactions.days_requested = 30` (= `INITIAL_SYNC_DAYS`), fixed at
  link time (TXN-009).
- Environment: the Plaid secret's `env` (`sandbox` | `production`) is the single
  source of truth for both the API base URL and the Link session (GUIDE-004).

## Human tasks

| Task | Why it matters | Where | State |
|------|----------------|-------|-------|
| Production access for Transactions (US) | Link token creation fails with INVALID_PRODUCT without it | https://dashboard.plaid.com/settings/team/products | verified (dashboard: production authorized, `transactions` in US production) |
| Put production `client_id` / `secret` into the `family-finances/plaid` secret with `env=production` — run `python scripts/plaid_link.py set-credentials` (prompts, never echoes) | Production calls fail; sandbox tokens are invalid in production | https://dashboard.plaid.com/developers/keys | needs_human |
| Finish the Data Transparency Messaging use case for the `default` Link customization | Link token creation fails with INVALID_LINK_CUSTOMIZATION (PITFALL-002) | https://dashboard.plaid.com/link/data-transparency-v5 | cannot_verify |
| Complete the company profile and the security questionnaire (Chase, PNC and other OAuth banks are hidden from Link without them) | Those banks silently missing from Link (OAUTH-008, TASK-006) | https://dashboard.plaid.com/settings/company/profile , https://dashboard.plaid.com/settings/company/compliance | cannot_verify |
| Dashboard webhook receivers | Not needed: the per-Item webhook is set on every link token (WEBHOOK-002) | https://dashboard.plaid.com/developers/webhooks | not_applicable |
| OAuth redirect URI allowlist | Not needed: Hosted Link hosts the OAuth redirect itself and no `completion_redirect_uri` is used | https://dashboard.plaid.com/developers/api | not_applicable |
| Deploy the stack (push to `main`) | Webhook verification, async sync and alerts only run once deployed | GitHub Actions *Deploy* | needs_human |

## Implementation checklist

- [x] `shared/plaid.py`: `/link/token/create` (Hosted Link, new + update mode),
      `/link/token/get`, `/item/get`, `/item/remove`, `/institutions/get_by_id`,
      `/webhook_verification_key/get`.
- [x] `shared/plaid_webhook.py`: Plaid-Verification JWT check — ES256 only, key
      by `kid` cached until `expired_at`, `iat` ≤ 5 min, SHA-256 of the raw body
      compared in constant time (WEBHOOK-003/004). PyJWT[crypto] in the layer.
- [x] Plaid webhook Lambda: verify, then hand the sync to an async self-invoke and
      return 200 immediately (WEBHOOK-001). Sync stays idempotent with the
      optimistic cursor lock (CHECK-010, TXN-002/004/007).
- [x] ITEM webhooks (ERROR, PENDING_DISCONNECT, PENDING_EXPIRATION,
      USER_PERMISSION_REVOKED, LOGIN_REPAIRED, NEW_ACCOUNTS_AVAILABLE) and
      user-action sync errors (ITEM_LOGIN_REQUIRED etc.) set `status` on the
      PlaidItems row and send one Telegram alert per status change with the
      command to run (WEBHOOK-006, ITEM-003/004/005).
- [x] `scripts/plaid_link.py`: `set-credentials`, `link`, `update`, `list`,
      `remove`. Exchanges server-side (the CLI is the trusted backend, using the
      developer's AWS credentials), persists the access token to Secrets Manager
      before anything else (GUIDE-015), duplicate-institution check (ITEM-011),
      update mode never re-exchanges (ITEM-003), `/item/remove` on unlink
      (ITEM-005), triggers the first sync at link time (TXN-003). Never prints
      tokens or secrets; logs `request_id` / `link_session_id` (GUIDE-006).
- [x] Tests for the client, webhook verification, alerts and CLI helpers.

## Acceptance

- Round 0: pending.

## Maintenance

This integration is maintained with the Plaid MCP; this file is its durable
state. Agents working in this repository: consult build_guidance before
modifying any Plaid-touching code, and re-run build_check_acceptance before
reporting such a change as working. The acceptance above applies only to
the code as it was verified — later changes invalidate it.

## How to test

### Link a bank (sandbox first, then production)

1. Deploy (push to `main`) and wait for the *Deploy* workflow.
2. `python scripts/plaid_link.py link`
3. Open the printed URL. In sandbox pick First Platypus Bank and log in with
   `user_good` / `pass_good` (or `user_transactions_dynamic` / any password).
   In production, pick your real bank.
4. Return to the terminal and press Enter. Expected: `Linked item <id> at
   <bank>: N accounts` and `First sync requested.` Transactions show up in
   Telegram on the next send run.
5. `python scripts/plaid_link.py list` shows the Item with `status ok`.

### Reconnect a broken login

1. Sandbox: `python scripts/plaid_sandbox.py reset-login <item_id>` → Telegram
   alert "needs to be reconnected" with the command.
2. `python scripts/plaid_link.py update <item_id>`, finish in the browser, press
   Enter. Expected: `Reconnected`, status back to `ok`, sync requested.

### Unlink

1. `python scripts/plaid_link.py remove <item_id> [--purge-transactions]`.

## Decisions made

- Link surface: local CLI + Hosted Link (developer, 2026-10-10).
- CLI commands `set-credentials` / `link` / `update` / `list` / `remove` in
  `scripts/plaid_link.py` (developer approved the preview, 2026-10-10).
- History: 30 days (developer, 2026-10-10).
- Add webhook signature verification and ITEM-error Telegram alerts
  (developer, 2026-10-10).
- `client_user_id` is the fixed string `family` — a single-household app.

## Open questions

- None.
