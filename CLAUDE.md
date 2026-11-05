# Claven

Claven is a Gmail labeling service. It applies user-defined label rules to incoming email, using Gmail's History API for incremental processing and a known-senders cache (built from Sent mail) to distinguish first-time senders.

It is being migrated from a local script to a stateless SaaS service. See `SAAS_TODOS.md` for the full plan.

## Current state of the codebase

The code is still in its pre-migration, flat-script form:

- `main.py` — entry point and business logic (mixed together, to be separated)
- `gmail_service.py` — Gmail API calls and file-based state
- `labeler.py` — label rule matching
- `config.yaml` — label rules (per-user rules will move to the DB)

The target structure is a `claven/` package — see `SAAS_TODOS.md` "Code restructure" section. **Do not add new features to the flat scripts.** New work goes into the `claven/` package shape.

## Architectural constraints

These apply to all code in this repo. Violating them undermines the SaaS architecture.

**1. No logic in entry points.**
`cli.py`, `server.py`, and `job.py` are thin wrappers. They parse input (CLI args, HTTP requests, env vars), call `claven/core/`, and format output. Business logic — anything that touches Gmail, the database, or label rules — belongs in `claven/core/`.

**2. The CLI is a thin HTTP client. It never imports core directly.**
`cli.py` makes HTTP requests to `$CLAVEN_SERVER_URL`. It does not call `claven/core/` functions. For local development, `CLAVEN_SERVER_URL` points at a local `server.py` instance. This keeps core logic in one place and eliminates version drift.

**3. All state lives in Neon. No file I/O for application state.**
The `accounts/` directory and `*.json` checkpoint files are the pre-migration approach. In the new architecture, all reads and writes go through `claven/core/db.py`. Do not add new file-based state.

**4. Operations must be idempotent.**
Push, pull, and poll modes can process the same user concurrently. Label application must check before applying. `historyId` updates must use optimistic or pessimistic locking at the DB level (`SELECT ... FOR UPDATE SKIP LOCKED`).

**5. No secrets in code or config files.**
Secrets (OAuth client secret, Neon connection string, token encryption key) are injected via environment variables at runtime. Do not hardcode or commit credentials.

## Development approach

This project follows test-driven development. **Tests are written before implementation.** Do not write implementation code for a feature or module without a corresponding test written first. The test plan lives in `tests/README.md`.

## Debug logging

Every function with control flow (if/elif/else, loops, early returns) must have a `logger.debug()` call in each basic block after every branch point. This makes it possible to trace the exact code path taken from the log output alone. When adding or modifying control flow, add debug log lines that include the function name, the branch taken, and the key variable values that determined the branch.

## Active branch

`saas` is the active development branch. `main` is protected — PRs only, no direct pushes.

## Git workflow

Before opening any PR, rebase the branch onto its base branch:

```
git fetch origin && git rebase origin/main && git push origin <branch> --force-with-lease
```

## Post-deploy verification

After merging any PR to main, **verify the Deploy workflow succeeded** before moving on. The deploy includes a smoke test that hits `/healthz` and `/api/me` on prod — if it fails, prod sign-in is broken.

```
gh run list --branch main --workflow=deploy.yml --limit 1
```

If the deploy fails, check logs immediately. Do not merge another PR until the deploy is green.

## Test accounts

**`claven.test.inbox@gmail.com` is reserved exclusively for automated e2e tests.** Do not sign in with it manually or treat any state it accumulates as persistent. The `clean_test_user` fixture in `tests/e2e/` deletes this account from the DB before and after every test run, so any manually-configured state will be wiped the next time CI runs.

To re-obtain a refresh token for this account (e.g. after token expiry), run `scripts/get_test_token.py` and update the `TEST_GMAIL_REFRESH_TOKEN` GitHub secret.

## Live test concurrency

`claven.test.inbox@gmail.com` supports only **one active Gmail push watch** at a time. Concurrent `live` test runs will corrupt each other's `historyId` and cancel each other's watch in teardown.

**Deploy is serialized** via `concurrency: group: live-gmail-tests` in `deploy.yml`, the only workflow that runs `live` tests — GitHub Actions queues overlapping runs rather than running them in parallel. `ci.yml` runs only `-m "not live"` tests and uses its own per-branch group, so CI runs (e.g. a batch of Dependabot PRs) can't cancel a queued deploy.

**Local `make ci` is not serialized with GitHub CI.** Do not run `make ci` locally while a CI run is active on GitHub. Check GitHub Actions before starting a local CI run.

## Key decisions

- **Serverless platform:** Cloud Run (`min-instances=0` to start; upgrade to 1 when interactive CLI latency matters)
- **Database:** Neon Free plan Postgres with PgBouncer connection pooling, in its own Neon org (`claven`, no card) with compute capped at 0.25 CU. Free never bills but suspends the project for the rest of the month at 100 CU-hours or 5 GB transfer; `.github/workflows/neon-usage.yml` checks the pace every six hours and opens a "Neon usage" issue on a warning
- **Notification modes:** poll is primary: Cloud Scheduler calls `/internal/poll` every 20 minutes. Gmail push notifications (Pub/Sub) still arrive at `/webhook/gmail`, which verifies and acknowledges them without touching the database, because each push woke Neon and the Free plan's compute budget can't afford hundreds of wakes a day
- **Initial scan vs. incremental processing:** the one-time scan uses `list_messages`; all incremental processing uses `list_history` since a stored `historyId` — these are distinct code paths, never conflated
