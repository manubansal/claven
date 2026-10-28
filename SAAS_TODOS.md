# Claven — SaaS Migration Todos

## Architecture

### Execution model: stateless + serverless

The service is fully stateless — no filesystem state between invocations. All state lives in Neon (free plan Postgres). This enables a true scale-to-zero serverless model on Cloud Run where containers only run when there is actual work to do.

The polling loop in `main.py` (`while running: shutdown_event.wait(...)`) is replaced by event-driven execution. Three notification modes are supported for reliability and flexibility:

| Mode | Trigger | Requires public endpoint? | Latency | Use case |
|---|---|---|---|---|
| **Push** | Pub/Sub push → Cloud Run webhook | Yes | Near-realtime | Production |
| **Pull** | Cloud Scheduler → Cloud Run pulls Pub/Sub | No | Scheduled interval | Fallback, local dev |
| **Poll** | Cloud Scheduler → Cloud Run polls Gmail history API | No | Scheduled interval | No Pub/Sub, local dev |

All three modes run the same processing logic — the trigger mechanism is fully swappable because the service is stateless and all state (including `historyId`) lives in Neon.

### Push mode (primary)

1. On user signup, call `gmail.users.watch()` to register a Pub/Sub topic for their inbox
2. Gmail publishes a notification to the Pub/Sub topic when new mail arrives
3. Pub/Sub **push subscription** POSTs to the Cloud Run webhook endpoint
4. Cloud Run spins up, fetches Gmail history since stored `historyId`, processes messages, updates Neon, terminates
5. Pub/Sub retries automatically on non-2xx with exponential backoff for up to 7 days
6. Gmail watches expire after 7 days — `/internal/renew-watches` (Cloud Scheduler, daily) renews them via `start_watch()` (idempotent: extends existing watch or creates a new one)

### Pull mode (fallback)

Same Pub/Sub topic, second **pull subscription**. Cloud Scheduler triggers Cloud Run on a configurable interval; the instance pulls all pending messages from the subscription, processes them, and exits. Works without a public endpoint — useful when the push endpoint is unavailable or for local dev against a real inbox.

### Poll mode (direct fallback)

No Pub/Sub involved. Cloud Scheduler triggers Cloud Run on a configurable interval; the instance calls `gmail.users.list_history()` directly since the stored `historyId`, processes new messages, updates Neon, exits. Fully self-contained — useful for local development without any GCP infrastructure.

### Execution primitives

| Primitive | What it handles |
|---|---|
| **Cloud Run service** | Push webhook handler, OAuth endpoints, user dashboard, watch renewal, pull/poll trigger endpoints |
| **Cloud Run Job** | One-off initial inbox scan + sent recipients scan on signup — uses `list_messages`, not `list_history`; runs to completion with no timeout constraint |
| **Cloud Scheduler** | Two jobs: (1) `/internal/renew-watches` daily — renews Gmail watches via idempotent `start_watch`; (2) `/internal/poll` hourly — polls for missed messages via `list_history`, reconciles sent scan if needed |

The initial scan (Cloud Run Job) and the notification modes (push/pull/poll) are distinct code paths. The scan walks the full inbox once and establishes the `historyId`. The notification modes only process incremental history since that `historyId` and are never used for bootstrapping.

### Code structure: core library + thin entry points

All business logic lives in a `claven/core/` package. The CLI, web server, and Cloud Run Job are thin entry points that call into the core — they handle how work is triggered, not what the work is. No logic lives in entry points; no logic is duplicated across them.

```
claven/
  core/
    scan.py      # initial_scan, sent_recipients (list_messages-based, one-time)
    process.py   # process_message, run_poll, run_pull (list_history-based, incremental)
    watch.py     # gmail.users.watch / stop / renew
    db.py        # all Neon reads and writes
    gmail.py     # Gmail API calls (from gmail_service.py)
    rules.py     # label matching (from labeler.py)
    auth.py      # token storage, refresh, OAuth flow helpers

  cli.py         # click entry point — thin HTTP client to server.py
  server.py      # FastAPI/Flask entry point — parse HTTP, call core, return JSON
  job.py         # Cloud Run Job entry point — call core, exit
```

The CLI is a **thin HTTP client to the server** — it does not import or run `claven/core/` directly. This means:
- No business logic in the CLI layer
- No version drift between CLI and server (core only runs server-side)
- Concurrency is handled in one place (the server)
- Local dev: point `CLAVEN_SERVER_URL` at a local `server.py` instance
- Production: point `CLAVEN_SERVER_URL` at the Cloud Run service URL

`claven poll --once` POSTs to `$CLAVEN_SERVER_URL/internal/poll`. `POST /internal/poll` calls `core.process.run_poll()`. Same execution path regardless of caller.

### Serverless platform: Cloud Run

**Current decision: Cloud Run.** Alternatives considered: Fly.io.

Rationale for Cloud Run:
- Native integration with Pub/Sub, Cloud Scheduler, Secret Manager, Artifact Registry — all already in the stack
- Generous free tier (2M requests/month, 360K GB-seconds, 180K vCPU-seconds) — sufficient for early stage
- Pay-as-you-go beyond free tier; no hard billing cap built in (see billing risk below)

Fly.io is a viable alternative if billing risk or GCP ecosystem lock-in becomes a concern — it has always-on free instances and configurable spending limits. Revisit if Cloud Run costs become an issue.

**Billing risk:** GCP budget alerts notify but do not stop services. A retry storm or DDoS could generate unexpected charges. Mitigations: set a GCP budget alert at a low threshold ($5-10), consider a Cloud Function to disable billing if the threshold is breached. Verify current GCP billing safety options before going to production.

### Latency model

The CLI talks to the server over HTTP. End-to-end latency depends on the state of two independent components: the Cloud Run container and the Neon database.

| Config | Container | Neon | Typical latency | Notes |
|---|---|---|---|---|
| **cold-cold** | Cold | Suspended | 4–7s | Both need to wake up; worst case |
| **cold-warm** | Cold | Active | 3–6s | Container dominates; Neon was recently used |
| **warm-cold** | Warm | Suspended | ~500–600ms | Neon free tier suspends after 5 min idle |
| **warm-warm** | Warm | Active | 25–70ms | Fully warm path; excellent interactive latency |

**Container warm window:** Cloud Run keeps a container warm for approximately 15 minutes after the last request (not guaranteed; platform-managed). Within a session, only the first command pays the cold start cost.

**Neon suspension:** Neon free tier suspends the database after 5 minutes of inactivity. Wake time is ~500ms. Paid Neon plans allow disabling suspension.

**min-instances configuration:**
- `min-instances=0` (default): scale to zero, free tier, cold-cold or cold-warm on first request per session
- `min-instances=1`: one container always warm, eliminates container cold start, ~$10–15/month — defer until interactive UX is a priority for paying users

For Pub/Sub webhooks and Cloud Scheduler triggers, cold start latency is acceptable — they are not interactive. The cold start cost only matters for CLI use.

### Secrets and config

All secrets (Neon connection string, OAuth client secret, token encryption key) are stored in Google Secret Manager and mounted as environment variables. Nothing sensitive is baked into the container image.

---

## Test plan (do this before everything else)

Before any implementation work begins — including the code restructure — write a test plan and implement the test scaffolding. New functionality is written test-first: tests are written before the implementation they cover.

### Directory structure

```
tests/
  unit/          # pure logic, no I/O — runs in milliseconds
  integration/   # real DB, mocked Gmail API — runs in seconds
  server/        # HTTP layer via test client — no real server needed
  e2e/           # CLI against a running local server — slowest, run on merge only
  fixtures/      # shared DB, Gmail, and user factories
  conftest.py    # top-level shared fixtures
  README.md      # test plan documentation (written before any implementation)
```

### Frameworks and libraries

| Concern | Library | Reason |
|---|---|---|
| Test framework | `pytest` | Standard |
| Async support | `pytest-asyncio` | FastAPI encourages async |
| Test DB | `pytest-postgresql` | Real Postgres, no Docker dep, self-contained |
| DB transactions | Per-test rollback fixture | No state leaks between tests |
| Schema setup | Alembic migrations at session start | Tests always run against the real schema |
| Gmail mocking | `FakeGmailService` class | Full control, inspectable, no VCR fragility |
| Server testing | `httpx` + ASGI transport | Test HTTP layer without a real server |
| CLI HTTP mocking | `respx` | Intercepts outbound CLI requests for unit-level CLI tests |
| Time mocking | `freezegun` | Token expiry, watch expiry, scheduler tests |
| Test data | `pytest` fixtures + factory functions | Keep it simple; add `factory_boy` if complexity grows |

### Key fixtures

**Database fixture** — `pytest-postgresql` spins up a real Postgres process per test session. Each test runs inside a transaction that rolls back on teardown — no state leaks. Alembic migrations run once at session start so tests always run against the live schema.

**`FakeGmailService`** — a class implementing the same interface as `claven/core/gmail.py`. Seeded with messages, history records, and sent recipients per test. Tracks all calls (so tests can assert `apply_label` was called with the right args). Simulates errors: 429 rate limit, 404 expired `historyId`, network failure.

### CI strategy

- **Unit + integration + server tests**: run on every PR — must pass before merge
- **E2E tests**: run on merge to `main` only — too slow for every PR
- **Test DB in CI**: GitHub Actions `services` block with a Postgres container

### Setup tasks

- [ ] Add `pytest`, `pytest-asyncio`, `pytest-postgresql`, `httpx`, `respx`, `freezegun` to `requirements-dev.txt`
- [x] Set up `pytest.ini` / `pyproject.toml` with test paths, async mode, and markers (`unit`, `integration`, `server`, `e2e`)
- [ ] Write `tests/conftest.py` with shared DB session fixture (per-test rollback) and `FakeGmailService` fixture
- [ ] Write `tests/fixtures/db.py` — test user factory, token factory, scan state factory
- [ ] Write `tests/fixtures/gmail.py` — `FakeGmailService` with seedable messages, history, sent recipients, and call tracking
- [x] Run Alembic migrations against test DB at session start
- [ ] Write `tests/README.md` documenting the test plan before any implementation begins
- [x] Add unit + integration + server tests to GitHub Actions CI; E2E tests gated to merge-to-main only

### Live test concurrency

`claven.test.inbox@gmail.com` supports only one active Gmail push watch. To enable concurrent live test runs (multiple CI jobs, local + CI simultaneously), each run needs its own Gmail account.

**Option A — Static account pool + shared lease table (recommended first step)**

Pre-create N Gmail accounts (`claven.test.1@gmail.com`, …), store their refresh tokens as secrets, and coordinate via a lease table in the shared Neon DB:

- [ ] Create 3–5 `claven.test.N@gmail.com` accounts; run `scripts/get_test_token.py` for each to obtain a refresh token
- [ ] Add GitHub Actions secrets `TEST_GMAIL_ACCOUNT_1` … `TEST_GMAIL_ACCOUNT_N` (JSON blob with `email` + `refresh_token`)
- [ ] Write Alembic migration: `test_account_leases(email PK, leased_by TEXT, leased_at TIMESTAMPTZ, expires_at TIMESTAMPTZ)` — seed with the pool accounts on migration
- [ ] Add `claim_test_account` / `release_test_account` helpers in `tests/fixtures/gmail_accounts.py` using `SELECT ... FOR UPDATE SKIP LOCKED` against the shared Neon DB; auto-expire leases older than 10 minutes
- [ ] Replace hardcoded `TEST_GMAIL_EMAIL` / `TEST_GMAIL_REFRESH_TOKEN` in `test_signup_live.py` with the claimed account; release in teardown
- [ ] Update CI workflow to pass the pool secrets and the Neon `DATABASE_URL` (for lease coordination, separate from the ephemeral test DB)

**Option B — Google Workspace Admin SDK (scalable, requires domain)**

With a Google Workspace org (e.g. `test.claven.app`) each test run creates and deletes a throwaway `run-{uuid}@test.claven.app` account on demand — unlimited concurrency, no lease coordination needed:

- [ ] Set up a Google Workspace org on a test subdomain (e.g. `test.claven.app`); provision an admin service account with Directory API access
- [ ] Write `tests/fixtures/gmail_accounts.py`: `create_throwaway_account()` / `delete_throwaway_account()` via Admin SDK `users.insert` / `users.delete`
- [ ] Add session-scoped fixture that creates the account, waits for Gmail API propagation (~60s), yields credentials, then deletes the account in teardown
- [ ] Update `test_signup_live.py` to use the throwaway account instead of the shared inbox
- [ ] Store the admin service account key as a GitHub Actions secret; document the Workspace setup

### Graceful shutdown tests — `server.py` + `core/scan.py`

- [ ] Tests for `_interruptible_sleep` (fallback to `time.sleep`, event.wait, instant wake on set event, wake on mid-sleep set)
- [ ] Tests for `_is_current_worker` (True normally, False when shutdown event set, False when PID changed)
- [ ] Tests for `_shutdown_handler` (sets event, idempotent)
- [ ] Tests for `_spawn_scan_thread` (daemon flag, thread registry, start, arg passthrough)
- [ ] Tests for lifespan startup (clears event + thread list)
- [ ] Tests for lifespan shutdown (sets event, joins threads with timeout, clears list, warns on stuck threads)
- [ ] Integration: spawned thread exits on lifespan shutdown (full flow)
- [ ] Tests for `build_known_senders`/`scan_inbox` passing `shutdown_event` to `_interruptible_sleep`

See full plan: `docs/graceful-shutdown-test-plan.md`

### Unit test cases — `core/rules.py`

- [x] Rule matches on `From` header
- [x] Rule matches on `Subject` header
- [x] `known_senders` condition — known sender labelled differently from unknown
- [x] Multiple rules — all matching rules applied
- [x] No rules match — no label applied, no error
- [x] Empty rules list — no error
- [ ] Regex pattern in rule — matches and non-matches

### Integration test cases — `core/process.py`

- [x] New message arrives, matching rule → label applied
- [x] New message, no matching rule → nothing applied, no error
- [x] Message already has the label → `apply_label` not called again (idempotent)
- [x] No new messages since `historyId` → no-op, `historyId` unchanged
- [x] `historyId` expired (fake Gmail returns 404) → falls back to full scan
- [x] Two concurrent processes on same user → second skips via `SKIP LOCKED`, no duplicate processing

### Integration test cases — `core/scan.py`

- [x] Full scan processes all messages, writes `historyId` to DB on completion
- [x] Scan interrupted mid-way → checkpoint saved; resume picks up where it left off, no duplicates
- [x] `known_senders` cache built correctly from Sent mail
- [ ] `--max-messages` limit respected
- [ ] Re-run after `known_senders` grew → reprocesses messages to apply new labels

### Integration test cases — `core/db.py`

- [x] CRUD for all tables (`users`, `gmail_tokens`, `sent_recipients`, `scan_state`)
- [ ] Optimistic locking: two processes read same `historyId`, first write wins, second detects conflict
- [x] `SKIP LOCKED`: locked user row is skipped, not blocked indefinitely

### Live e2e test token

- [ ] Refresh `TEST_GMAIL_REFRESH_TOKEN` GitHub secret — current token expired, live e2e test skipped in CI (`-m "not live"` in ci.yml). Re-enable live tests after refreshing.

### Gmail API call correctness — outgoing query assertions

- [ ] Assert exact parameters on all Gmail API calls issued by `/api/me` (labels.get IDs, messages.list queries, messages.get format)
- [ ] Assert exact parameters on all Gmail API calls issued by `scan_inbox` and `build_known_senders`
- [ ] Assert exact parameters on all Gmail API calls issued by `/webhook/gmail` and `/internal/poll`

### Server test cases — `server.py`

- [x] `/webhook/gmail` — valid Pub/Sub notification → processing triggered
- [x] `/webhook/gmail` — missing or invalid `Authorization` header → 401, no processing
- [x] `/webhook/gmail` — malformed payload → 400, no processing
- [x] `/internal/poll` — unauthenticated → 401; authenticated → iterates all active users
- [ ] `/internal/pull` — pulls from fake Pub/Sub subscription, processes pending notifications
- [ ] `/internal/scan` — triggers Cloud Run Job for the specified user
- [ ] `/internal/renew-watches` — renews all expiring watch subscriptions
- [ ] `/healthz` — returns 200 with DB up; returns 503 with DB unreachable
- [x] OAuth `/oauth/start` → redirect contains `state` parameter
- [x] OAuth `/oauth/callback` — valid code → tokens stored; missing `state` → 400

### E2E test cases — `cli.py`

- [ ] `claven status --output json` → valid JSON shape, correct data
- [ ] `claven poll --once --output json` → progress events stream to stdout, final summary on completion
- [ ] `claven auth status --output json` → token validity and expiry reported correctly
- [ ] Server down → exit code 2, JSON error on stderr
- [ ] Missing `CLAVEN_SERVER_URL` → exit code 1, usage error on stderr

## Code restructure (do this first, after test plan)

Everything else depends on this shape. Current `main.py`, `gmail_service.py`, and `labeler.py` are flat scripts — restructure into a `claven/` package before adding any new functionality.

- [x] Create `claven/` package with `core/` subpackage
- [x] Move `labeler.py` → `claven/core/rules.py` (no logic changes)
- [x] Move Gmail API calls from `gmail_service.py` → `claven/core/gmail.py` (no logic changes)
- [x] Extract `initial_scan` and sent recipients logic from `main.py` → `claven/core/scan.py`
- [x] Extract `process_message`, `poll_new_messages` from `main.py` → `claven/core/process.py`
- [x] Create `claven/core/db.py` as a stub (file-based for now, swapped for Neon later)
- [x] Create `claven/core/auth.py` as a stub (wraps current `get_service` for now)
- [x] Create `claven/cli.py` as the new entry point — thin `click` wrapper over core functions, replacing `main.py`
- [ ] Create `claven/server.py` and `claven/job.py` as empty stubs so the shape is established
- [x] Verify the restructured code runs identically to the current `main.py` before proceeding

## Auth & OAuth

- [x] Create a new **Web application** OAuth client in GCloud (separate from the Desktop app one); add your server's callback URL as an authorized redirect URI
- [x] Replace `InstalledAppFlow.run_local_server()` with a proper web OAuth flow: `/oauth/start` redirects to Google, `/oauth/callback` exchanges the code for tokens
- [x] Add `state` parameter to the OAuth redirect and verify it on callback (CSRF protection)
- [x] Decide on user identity: "Sign in with Google" is simplest (email from the token's `id_token` or `userinfo` endpoint becomes the user ID), or support email/password separately
- [x] Store and refresh tokens server-side (see Database section); the current `token.json` approach doesn't work when the server holds tokens on behalf of users

## Database

- [x] Replace all file-based state with a database — every `accounts/<name>/*.json` file maps to a DB table:
  - `users` — id, email, created_at
  - `gmail_tokens` — user_id, access_token (encrypted), refresh_token (encrypted), expiry
  - `sent_recipients` — user_id, email_address (replaces `sent_recipients_cache.json`)
  - `scan_state` — user_id, history_id, resume_index, processed_message_ids, known_senders_count (replaces `scan_checkpoint.json`)
- [x] Encrypt tokens at rest — never store raw access/refresh tokens in the DB
- [x] Replace the file-based `load_scan_checkpoint` / `save_scan_checkpoint` and `_load_recipients_cache` / `_save_recipients_cache` functions with DB-backed equivalents

## Per-User Configuration

- [ ] Move `config.yaml` label rules into the database so each user can have their own rules
- [ ] Build a UI or API for users to manage their label rules (add/edit/delete)
- [ ] Keep polling interval configurable per user or as a server-wide setting (used by pull and poll modes)

## Initial Scan (one-time per user)

Runs **once on signup** using a different code path from all three notification modes. Walks the entire inbox via `list_messages` (not `list_history`), applies label rules to every existing message, and builds the `sent_recipients` cache from Sent mail. Can take minutes for large inboxes. After it completes, `historyId` is written to the DB and the user switches to incremental processing via push/pull/poll.

This is **not** the same as poll mode — poll uses `list_history` and only touches new mail. The initial scan uses `list_messages` and touches everything.

- [ ] Trigger a **Cloud Run Job** on signup to run the initial inbox scan and sent recipients scan — must not block the OAuth callback or webhook handler
- [x] Initial scan writes `historyId` to Neon on completion; this is the starting point for all subsequent incremental processing
- [x] Scan is resumable via checkpoint in Neon (already implemented with `scan_checkpoint.json` — migrate to DB)
- [ ] Support running via CLI for local use — see CLI spec: `claven scan --user <email>`

## Notification Modes

These all share the same incremental processing logic: `list_history` since stored `historyId` → apply rules → update `historyId`. They differ only in how processing is triggered. **None of these replace the initial scan** — they only handle new mail after the scan has completed and a `historyId` is established.

### Push (primary — Pub/Sub push subscription → Cloud Run webhook)

- [x] On user signup (after initial scan completes), call `gmail.users.watch()` to activate push notifications; `historyId` is already in DB from the scan
- [x] Create a Pub/Sub **push subscription** pointing to `/webhook/gmail` on the Cloud Run service
- [x] Implement `/webhook/gmail`: decode Pub/Sub notification, look up user by email, fetch Gmail history since stored `historyId`, process new messages, update `historyId` in DB
- [x] Return 2xx within Pub/Sub's delivery deadline (acknowledge before processing completes if needed, or process synchronously for small batches)
- [ ] `watch()` subscriptions expire after 7 days — Cloud Scheduler hits `/internal/renew-watches` every 6 days

### Pull (fallback — Pub/Sub pull subscription → scheduled Cloud Run)

- [ ] Create a second Pub/Sub **pull subscription** on the same topic (separate from the push subscription so both receive all notifications independently)
- [ ] Implement `/internal/pull`: pull all pending messages from the pull subscription, deduplicate by `historyId`, run incremental processing for each user, update Neon
- [ ] Create a Cloud Scheduler job to hit `/internal/pull` on a configurable interval (e.g. every 5 minutes) as a reliability backstop
- [ ] Pull mode should work without a public endpoint for local development — see CLI spec: `claven pull --once`

### Poll (direct fallback — scheduled Cloud Run polls Gmail history API)

- [x] Implement `/internal/poll`: iterate all active users, call `list_history()` since stored `historyId` for each, run incremental processing, update Neon
- [ ] Create a Cloud Scheduler job to hit `/internal/poll` on a configurable interval as a fallback when Pub/Sub is unavailable
- [ ] Poll mode must work fully locally with no GCP infrastructure — see CLI spec: `claven poll --once` or `claven poll --interval 60`
- [x] Remove the old `while running:` polling loop from `main.py` once poll mode is implemented via the new path

## CLI

### Design principles

- **`--output json`** (global flag) — all commands emit structured JSON to stdout instead of log lines; errors go to stderr as JSON
- **Fully headless** — no interactive prompts; all inputs are flags or environment variables
- **Exit codes**: 0 = success, 1 = usage/input error, 2 = runtime/API error
- **`DATABASE_URL`** env var is the primary way to point at Neon; overridable with `--db-url`
- **`--user <email>`** is required on most commands; omitting it operates on all users where it makes sense (e.g. `watch renew`, `poll`)
- Implemented with `click` (subcommand groups, composable, good help output)

### Output shapes

Success (stdout):
```json
{ "ok": true, "data": { ... } }
```

Error (stderr):
```json
{ "ok": false, "error": "<snake_case_type>", "message": "<human readable detail>" }
```

Progress events during long-running commands (stdout, one JSON object per line):
```json
{ "event": "progress", "processed": 120, "total": 1500, "pct": 8 }
```

### Command reference

#### `claven auth`

```
claven auth login  [--user <email>]
claven auth logout  --user <email>
claven auth status [--user <email>]
```

- `login` — starts the OAuth flow; prints the authorization URL for headless environments (no browser auto-open); exchanges the code and stores encrypted tokens in Neon
- `logout` — revokes tokens and deletes the user's row from `gmail_tokens`
- `status` — reports token validity and expiry for one user or all users

`auth status --output json` example:
```json
{
  "ok": true,
  "data": {
    "user": "user@example.com",
    "token_valid": true,
    "expires_at": "2026-04-24T10:00:00Z"
  }
}
```

#### `claven scan`

```
claven scan --user <email> [--max-messages N]
```

Runs the one-time initial inbox scan using `list_messages` (not `list_history`). Applies label rules to all existing inbox messages, builds the `sent_recipients` cache from Sent mail, writes `historyId` to Neon on completion. Resumable — safe to interrupt and re-run.

`scan --output json` streams progress events then a final summary:
```json
{ "event": "progress", "processed": 500, "total": 1500, "pct": 33 }
{ "event": "done", "processed": 1500, "labeled": 42, "history_id": "87654321" }
```

#### `claven watch`

```
claven watch start  --user <email>
claven watch renew [--user <email>]
claven watch stop   --user <email>
```

- `start` — calls `gmail.users.watch()`; requires a `historyId` already present in Neon (i.e. scan must have run first); stores watch expiry in DB
- `renew` — renews expiring subscriptions; operates on all users if `--user` is omitted; safe to run on a schedule
- `stop` — calls `gmail.users.stop()` and clears the watch record from DB

#### `claven pull`

```
claven pull [--user <email>] [--once]
```

Pulls pending messages from the Pub/Sub pull subscription, runs incremental processing (`list_history` since stored `historyId`) for each notified user, updates Neon. With `--once`, exits after draining the current backlog. Without `--once`, loops at the configured interval (for local development use).

#### `claven poll`

```
claven poll [--user <email>] [--once] [--interval N]
```

Polls the Gmail History API directly — no Pub/Sub required. Iterates all active users (or one if `--user` is given), calls `list_history` since stored `historyId`, processes new messages, updates Neon. `--interval N` runs continuously every N seconds (default 60). `--once` exits after a single pass.

#### `claven status`

```
claven status [--user <email>]
```

Shows current state for one user or all users: token validity, last processed timestamp, current `historyId`, watch expiry, scan completion status.

```json
{
  "ok": true,
  "data": [{
    "user": "user@example.com",
    "scan_complete": true,
    "history_id": "87654321",
    "last_processed_at": "2026-04-23T09:15:00Z",
    "watch_expires_at": "2026-04-30T09:00:00Z",
    "token_valid": true
  }]
}
```

#### `claven rules` _(future)_

```
claven rules list   --user <email>
claven rules add    --user <email> --name <label> [--from <pattern>] [--subject <pattern>]
claven rules delete --user <email> --name <label>
```

Manages per-user label rules stored in Neon. Not blocking on initial implementation — `config.yaml` rules can be migrated to DB as a later step.

### Implementation tasks

- [ ] Set up `click` with a top-level group and subcommand groups (`auth`, `scan`, `watch`, `pull`, `poll`, `status`, `rules`)
- [ ] Implement global `--output json` flag and a shared output helper that switches between log lines and JSON
- [ ] Implement global `--server-url` flag (default: `$CLAVEN_SERVER_URL`) — all commands route to this base URL
- [ ] Implement shared HTTP client with JSON error handling: maps non-2xx responses to stderr JSON + exit code 2
- [ ] Implement streaming response handling for long-running commands (`scan`, `pull`, `poll`) — server sends newline-delimited JSON progress events, CLI forwards them to stdout
- [ ] Implement `auth login`, `auth logout`, `auth status`
- [ ] Implement `scan`
- [ ] Implement `watch start`, `watch renew`, `watch stop`
- [ ] Implement `pull --once` and `pull` (loop)
- [ ] Implement `poll --once`, `poll --interval N`
- [ ] Implement `status`
- [ ] Implement `rules list`, `rules add`, `rules delete` _(can follow DB migration)_

## Security

### CLI → server authentication
- [ ] Decide on an authentication mechanism for CLI requests to the server (API key, JWT, or OAuth token)
- [ ] Implement server-side verification of CLI requests on all `/internal/*` and user-scoped endpoints
- [ ] Document how a user obtains and stores their credentials for the CLI (env var? `~/.claven/credentials`?)

### Webhook verification
- [x] Verify that Pub/Sub push requests to `/webhook/gmail` carry a valid Google service account token in the `Authorization` header
- [x] Reject unauthenticated webhook calls with 401 before any processing occurs

### Multi-tenancy isolation
- [ ] All DB queries must be scoped to the authenticated user — no cross-user data access possible at the query level
- [ ] Audit all `core.db` queries before going to production to confirm user_id scoping is enforced everywhere

### Rate limiting
- [ ] Add rate limiting to all server endpoints to prevent abuse and runaway retry storms
- [ ] Apply stricter limits to OAuth and auth endpoints

## Observability

- [ ] Define a structured logging strategy — log format, fields (user_id, request_id, event type), log levels
- [ ] Integrate with Cloud Logging — ensure structured logs from Cloud Run are queryable per-user and per-event
- [ ] Add an error tracking integration (e.g. Sentry) for unhandled exceptions in production
- [ ] Add a `/healthz` health check endpoint — required by Cloud Run to verify the service is alive; should check DB connectivity

## Database migrations

- [x] Set up Alembic for schema migrations — all schema changes go through migration files, never applied manually
- [x] Version the schema from the start; first migration creates the initial tables
- [ ] Document the migration workflow: run migrations on deploy before the new container starts serving traffic
- [x] Ensure migrations are safe to run against a live DB (additive changes, no destructive migrations without a plan)

## Web Server

- [x] Add a web framework (FastAPI, Flask, Django — pick one) to serve:
  - OAuth start/callback endpoints
  - User dashboard (connected accounts, label rules, status)
  - `/webhook/gmail` — Pub/Sub push handler
  - `/internal/scan` — trigger initial inbox scan Cloud Run Job for a new user
  - `/internal/pull` — pull pending Pub/Sub messages (triggered by Cloud Scheduler)
  - `/internal/poll` — poll Gmail history directly (triggered by Cloud Scheduler)
  - `/internal/renew-watches` — renew all active `watch()` subscriptions
- [x] Implement session management (JWT or server-side sessions)

## Dashboard

- [x] Sign-up/sign-in shows connected and ready to start filtering (not filtering yet)
- [x] Show count of total unread messages in inbox
- [x] Update status to full scan in progress when running the initial scan (spinners)
- [x] Update status to live scan when initial scan finishes (green play icon)
- [x] Display current rule configuration in info box
- [x] Show number of known senders
- [x] Display number of emails processed, filtered-in, filtered-out
- [x] Show last email processed at (system timestamp + newest email date)
- [x] Show total filter-out percentage (noise reduced metric)
- [x] Add switch account button
- [x] Add Pub/Sub notifications for new sent messages (low-latency known senders updates)

## User Documentation

- [ ] Create user-facing documentation (help page or in-app guide) covering the following known behaviors:
  - **Threads vs. messages**: Gmail labels are applied to individual messages, not threads. A thread can contain messages with different labels (e.g., one message labeled `known-sender` and another `unknown-sender`). A thread appears in inbox as long as any of its messages has the INBOX label.
  - **Sent scan before inbox scan**: The system scans your Sent mail first to build a known senders list, then labels inbox messages. This ensures anyone you've emailed is recognized as a known sender.
  - **Inbox-only labeling**: Labels are only applied to messages currently in your inbox. Archived or trashed messages are not labeled, even if they match a rule.
  - **Noise reduced metric scope**: The "Noise reduced" percentage is based on all messages ever labeled (`allmail_labeled_unknown_count / allmail_labeled_total_count`), including messages that have since been archived or moved out of inbox. This gives a lifetime view, not an inbox-only view.
  - **Archive action**: "Archive unknown-sender" removes the INBOX label from individual messages, not entire threads. If a thread contains both known and unknown messages, only the unknown ones are archived — the thread stays in inbox.
  - **Relabeling after known senders change**: If you send an email to someone new, they become a known sender. However, their existing inbox messages that were already labeled `unknown-sender` are not automatically relabeled.

## Website & Marketing

### Decision: Option 1 — everything in the existing Next.js app

Marketing pages, docs, and app all in one repo/deployment. Content in markdown, styling in shared components, same shadcn design system across everything.

### Starting point

Use shadcn templates ([ui.shadcn.com/templates](https://ui.shadcn.com/templates)) as the base. Evaluate and potentially incorporate layout patterns from:
- **Taxonomy** (shadcn's own SaaS starter)
- **next-saas-stripe-starter** (landing, pricing, blog, dashboard, auth, billing)
- **Dub.co** (open source production SaaS, Next.js + shadcn)

### Tasks

- [ ] Set up Next.js route groups: `(marketing)/` for public pages, `(app)/dashboard` for authenticated pages
- [ ] Create shared marketing layout shell (header with logo + nav, footer)
- [ ] Adapt a shadcn template for the landing page (hero, features, CTA)
- [ ] Move `/how-it-works` into the marketing layout
- [ ] Add pricing page (when ready)
- [x] Add privacy policy and terms of service pages
- [ ] Ensure marketing layout header adapts: public shows nav + "Sign in", authenticated shows account actions
- [ ] Keep content as markdown, rendering via `react-markdown` + `remark-gfm`
- [ ] Same Tailwind tokens and shadcn components across marketing and app — one design system

## Google's App Verification

- [x] Add a privacy policy and terms of service page (required before submitting for verification)
- [ ] Submit the OAuth app for Google verification — `gmail.modify` is a restricted scope; unverified apps are capped at 100 users
- [ ] Plan for the CASA Tier 2 security assessment (required for sensitive Gmail scopes in production)

## Infrastructure (Cloud Run + Neon)

### Platform decision
- [x] **Serverless platform: Cloud Run** — chosen for native GCP integration (Pub/Sub, Scheduler, Secret Manager, Artifact Registry); free tier sufficient for early stage
- [ ] **Revisit if needed:** Fly.io is a viable alternative with always-on free instances and hard spending limits — evaluate if Cloud Run billing risk or cold start latency becomes a real problem for users
- [ ] Set up a GCP budget alert at $10/month to catch unexpected charges early; investigate Cloud Function billing kill-switch before going to production
- [ ] Decide on `min-instances` setting: start at 0 (free, cold starts); upgrade to 1 (~$10–15/month) when interactive CLI latency matters to paying users

### Neon (database)
- [ ] Create a Neon project and grab the connection string — free tier gives one Postgres database
- [ ] Store the connection string as a secret (not in source); inject via environment variable at runtime
- [ ] Enable Neon's connection pooling (PgBouncer) — Cloud Run spins up many short-lived instances, so direct connections would exhaust Postgres connection limits fast

### Cloud Run (app)
- [ ] Containerize the app — write a `Dockerfile` that installs dependencies and runs the web server
- [ ] Push the image to **Google Artifact Registry** (free tier covers storage for one region)
- [ ] Deploy to Cloud Run: set `min-instances=0` initially (scales to zero when idle); 256MB memory is likely sufficient for the webhook handler
- [ ] Store all secrets (Neon connection string, OAuth client secret, token encryption key) in **Google Secret Manager** and mount them as environment variables in the Cloud Run service

### Pub/Sub (push + pull subscriptions)
- [ ] Create a Pub/Sub topic; grant Gmail permission to publish (`gmail-api-push@system.gserviceaccount.com` needs `roles/pubsub.publisher`)
- [ ] Create a **push subscription** pointing to `/webhook/gmail` on the Cloud Run service; verify the domain with Google
- [ ] Create a **pull subscription** on the same topic for the scheduled fallback path
- [ ] Set message retention on the pull subscription to at least 1 hour so scheduled pulls don't miss notifications during brief outages

### Cloud Scheduler
- [ ] Watch renewal: every 6 days → `/internal/renew-watches`
- [ ] Pull fallback: configurable interval (default 5 min) → `/internal/pull`
- [ ] Poll fallback: configurable interval → `/internal/poll` (can be disabled when push is healthy)

### Local development setup
- [ ] Write a `docker-compose.yml` that runs the server and a local Postgres instance — single command to get the full stack running locally
- [ ] Document the local dev setup in README: how to set required env vars, run the server, point the CLI at it

### Senders awaiting response (LLM-based)
- [ ] For each sender with unread messages, classify whether any of their messages need an action/response from the user vs. purely informational (newsletters, notifications, FYIs, CCs)
- [ ] Aggregate at the sender level, not per-message: "alice@example.com needs a response (3 messages)" not "message X is actionable"
- [ ] Fetch message bodies via Gmail API (`format=full`), send to LLM API (Claude/Gemini) with action-detection prompt
- [ ] Dashboard: ranked list of senders awaiting response, similar to top known senders but filtered to action-required only
- [ ] User decides who to service first from the list — priority queue of people, not messages
- [ ] Scope: start with known senders (top senders list), extend to unknown-sender later
- [ ] Cost control: classify per-sender batch (all unread from one sender in one LLM call), cache results until new messages arrive, token budget per user
- [ ] Context: the primary question is "who needs something from me?" not "what category is this email?" — helps the user decide who to service first vs. who can wait

### Chrome extension for Gmail integration
- [ ] Chrome extension that adds a "Top Senders" panel inside Gmail UI
- [ ] Calls `/api/top-senders` using the user's session cookie
- [ ] Each sender is a clickable link that filters Gmail to their unread inbox messages
- [ ] Context: users shouldn't need to switch to the Claven dashboard to prioritize their inbox — the information should be available where they read email

### CLI scan trigger
- [ ] Add `POST /api/scan` endpoint that triggers the sent→relabel→label scan chain
- [ ] Respects cancel state and exclusive job ownership (same logic as /api/me retrigger)
- [ ] CLI calls this after `claven connect` or `claven scan` — doesn't depend on dashboard load
- [ ] Context: /api/me retrigger only fires when the dashboard loads; CLI needs an explicit trigger

### CLI packaging and installation
- [ ] Package `claven` as a PyPI package so users can install with `pip install claven`
- [ ] Consider a Homebrew formula or standalone binary (e.g. via PyInstaller) for users who don't want to manage a Python environment
- [ ] Document installation in README

### User offboarding
- [ ] Implement account deletion: revoke Gmail OAuth tokens, call `gmail.users.stop()`, delete all user rows from Neon
- [ ] Expose via CLI: `claven auth delete --user <email>`
- [ ] Expose via server: `DELETE /user` (authenticated)

### API versioning
- [ ] Define a versioning policy for the CLI ↔ server API contract (e.g. `/v1/` prefix, or a version header)
- [ ] Add a version handshake: CLI sends its version on every request; server rejects incompatible versions with a clear error message rather than silent breakage

### Inbox modes (Label only / Gatekeeper / Sunset)
Replaced the planned `auto_archive_unknown` boolean with a three-way per-user mode (PRs #113–#115).
- [x] `scan_state.inbox_mode` (`label_only` | `gatekeeper` | `sunset`), `sunset_hours`, `inbox_mode_changed_at` (migration 0013); existing `auto_archive_unknown` values mapped to `gatekeeper` / `label_only`
- [x] Gatekeeper: label scan and live labeling archive unknown-sender mail as they label it (the old auto-archive)
- [x] Sunset: unknown-sender mail stays in the inbox; `/internal/sunset` (Cloud Scheduler, every 15 min) archives it once it is older than the timer (4h/12h/24h/3d) and still unread and unstarred. Stateless Gmail search, no per-message state. Mail already in the inbox when the user switches gets a full timer from the switch.
- [x] Sunset countdown labels (`c/3d` … `c/1h`) on waiting mail, rounded up to a step ("gone within"), first one added at live labeling; removed when read, starred, or archived; all deleted when the user leaves Sunset (`sunset_countdown_active`, migration 0014)
- [x] Hourly countdown labels (`c/72h` … `c/1h`), only the hours up to the user's timer created; listed in the sidebar only while unread; `c/3d` renamed to `c/72h`
- [x] Countdown labels replace `c/u` while mail waits (no `c/u` + `c/24h` pair); mail goes back to `c/u` when read, starred, archived, or the user leaves Sunset. Scans, relabel, archive job and dashboard counts treat countdown labels as unknown
- [x] `POST /api/settings/inbox-mode`; `/api/me` returns `inbox_mode`, `sunset_hours`; dashboard "Inbox mode" section with the timer dropdown
- [ ] Drop `scan_state.auto_archive_unknown` and the legacy `/api/settings/auto-archive` endpoint once rollback to pre-#113 is no longer needed
- [x] Short label codes under `c/` (`c/k`, `c/u`, `c/3d` … `c/1h`, hidden `c/s`) with traffic-light colours; old names renamed in place on the next label lookup (`claven/core/labels.py`, `migrate_legacy_labels`)
- [ ] Once every user has been migrated, drop `LEGACY_NAMES` and the rename path

### Demote sender endpoint
- [ ] `POST /api/actions/demote-sender` — remove a sender from the known senders list (delete from `sent_recipients`)
- [ ] Relabel their messages from known-sender to unknown-sender (batch_swap_labels)
- [ ] Add a demote button/action to the top known senders list on the dashboard
- [ ] Run as an exclusive job through `_run_task` with progress tracking
- [ ] Context: user discovers a known sender they want to treat as unknown (e.g. marketing emails from a vendor they once emailed)

### Service health monitors

**Availability**
- [ ] `/healthz` uptime check — Cloud Run is responding and DB is reachable
- [ ] Frontend uptime — `claven.app` returns 200

**Gmail connectivity**
- [ ] Watch freshness — alert if any user's watch hasn't been renewed in 8+ days
- [ ] Webhook delivery — alert if no webhooks received in 1+ hours (when users are connected)
- [ ] Poll success rate — track `/internal/poll` failures vs successes

**Scan health**
- [ ] Stale scans — alert if any user's `inbox_scan_status` is `in_progress` for more than 10 minutes (orphaned)
- [ ] Error rate — track classified error codes over time, alert on spikes
- [ ] Scan completion — alert if scans keep cancelling without completing

**Data freshness**
- [ ] `last_fetched_at` staleness — alert if any connected user hasn't had a fetch in 2+ hours
- [ ] `last_labeled_at` staleness — alert if unlabeled messages exist but no labeling in 2+ hours

**Infrastructure**
- [ ] Neon DB connection health — pool exhaustion, connection timeouts, quota warnings
- [ ] Gmail API quota usage — track 429 rate-limit errors, alert before quota exhaustion
- [ ] Cloud Scheduler job health — alert if scheduled jobs fail to fire

**User-facing**
- [ ] Dashboard load time — track `/api/me` response time, alert on degradation
- [ ] OAuth flow success rate — track sign-in failures vs completions

### Frontend error instrumentation
- [ ] Build a consistent error code map shared between backend and frontend
- [ ] Instrument the dashboard with fine-grained error reporting: network errors, auth failures, API timeouts, CORS issues
- [ ] Show visible error states for each failure mode instead of silent fallback to "unauthenticated" page
- [ ] Context: debugging the prod sign-in failure (2026-05-06) was difficult because the frontend gave no indication of what was failing

### CI/CD
- [ ] Set up a GitHub Actions workflow: on push to `main`, run tests, build the Docker image, push to Artifact Registry, and deploy to Cloud Run (`gcloud run deploy`)
- [ ] Run database migrations as part of the deploy step, before the new container starts serving traffic

## What's Already Reusable

The Gmail API call logic and the rule engine are clean and don't need significant changes — they move into `claven/core/` as-is:
- `labeler.py` → `claven/core/rules.py` — pure logic, no changes needed
- `gmail_service.py` → `claven/core/gmail.py` — all API calls are reusable; only auth/token plumbing and file I/O need replacing
- `process_message`, `poll_new_messages`, `initial_scan` in `main.py` → `claven/core/process.py` and `claven/core/scan.py` — logic is sound; just need DB-backed state passed in instead of `data_dir`
