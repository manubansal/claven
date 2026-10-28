# Job Control Flow

How Claven coordinates scan loops, exclusive jobs, and cancellation.

## Job types

### Scan loops (continuous)

Run in sequence, triggered by `/api/connect`, `/api/me` retrigger, or `oauth_callback`:

1. **Sent scan** (`build_known_senders`) — scans sent mail, builds known senders list
2. **Relabel scan** (`relabel_scan`) — swaps unknown-sender → known-sender for newly discovered known senders
3. **Label scan** (`scan_inbox`) — labels unlabeled messages as known-sender or unknown-sender

These run as a chain: sent → relabel → label. Each checks `should_continue()` per batch and exits if cancelled.

### Exclusive jobs (one-off)

User-initiated actions that require no scan loops running:

- **Archive unknown-sender** — removes INBOX label from unknown-sender messages
- **Reset sent scan** — removes the `c/s` (sent-scanned) label, triggering a full re-scan

Exclusive jobs and scan loops cannot run concurrently for the same user. An exclusive job must cancel running scans before starting.

### Scheduled sweeps (outside job control)

Triggered by Cloud Scheduler, not by the user, and not subject to `cancel_state`:

- **Poll** (`/internal/poll`, hourly) — catch-up for missed push notifications
- **Renew watches** (`/internal/renew-watches`, daily) — extends Gmail push watches
- **Sunset sweep** (`/internal/sunset`, every 15 min) — for users in Sunset mode, archives unread, unstarred unknown-sender inbox mail older than the timer, then updates countdown labels (which replace `c/u` while mail waits); for users who left Sunset, puts `c/u` back on waiting mail and deletes the countdown labels

The Sunset sweep can overlap with scans or an Archive unknown-sender job. That is safe: removing INBOX and moving countdown labels are idempotent, and the sweep only touches unknown-sender mail.

## Cancel state machine

### State column

`scan_state.cancel_state TEXT` — per-user, stored in the database.

| State | Meaning |
|---|---|
| `NULL` | Clean slate — no cancel active |
| `cancel_scans` | Exclusive job pending or running — scan loops must exit |
| `cancel_job` | User cancelled the running exclusive job — everything must exit |

### State transitions

```
NULL ──────────→ cancel_scans        User clicks action button (archive, reset, etc.)
cancel_scans ──→ NULL                Exclusive job completes normally (finally block)
cancel_scans ──→ cancel_job          User clicks cancel on the running job
cancel_job ────→ NULL                Job exits (finally block)
cancel_job ────→ NULL                Lifespan startup after crash
```

### Who checks what

| Caller | Exits on `cancel_scans` | Exits on `cancel_job` |
|---|---|---|
| Scan loops (sent, relabel, label) | Yes | Yes |
| Exclusive jobs (archive, reset) | No — this state is for them | Yes |

```python
def _should_continue_scan(user_id):
    """For scan loops: exit on any non-NULL cancel state."""
    if not _is_current_worker():
        return False
    with db.get_connection() as conn:
        state = db.get_cancel_state(conn, user_id)
        return state is None

def _should_continue_job(user_id):
    """For exclusive jobs: exit only on cancel_job."""
    if not _is_current_worker():
        return False
    with db.get_connection() as conn:
        state = db.get_cancel_state(conn, user_id)
        return state != "cancel_job"
```

### Lifecycle: starting an exclusive job

Same sequence whether triggered by user click or resumed after crash:

1. **Set state to `cancel_scans`**: signals scan loops to exit
2. **Wait**: poll active threads until scan loops exit (short timeout)
3. **Spawn exclusive job**: job runs, checking `_should_continue_job` per batch
4. **Job completes**: `finally` block sets state to `NULL`

The state stays at `cancel_scans` for the entire duration of the job. This prevents `/api/me` from retriggering scan loops while the job is running.

### Lifecycle: cancelling an exclusive job

1. User clicks cancel → endpoint sets state to `cancel_job`
2. Running exclusive job sees `cancel_job` on next batch check → exits
3. Job's `finally` block sets state to `NULL`
4. Scan loops can retrigger on next `/api/me`

### Lifecycle: second action while first is running

If a user clicks a second action button while an exclusive job is running:
- The endpoint checks if any exclusive job has status `in_progress`
- Returns `{"ok": false, "detail": "another action is running"}`
- User must cancel the first action before starting the second

Only one exclusive job runs at a time.

## Lifespan startup behavior

| Cancel state on boot | Action | Rationale |
|---|---|---|
| `NULL` | Nothing | Clean slate |
| `cancel_scans` | **Keep** | An exclusive job was pending or running. `/api/me` will detect the incomplete job and resume it. Scans stay blocked. |
| `cancel_job` | **Clear to `NULL`** | User wanted to cancel, process crashed before the job could exit. The job is dead — clear the flag so scans can resume. |

## `/api/me` priority order

```
1. Check cancel_state:
   - cancel_scans → check for incomplete exclusive job → resume it
     (blocks all subsequent steps)
   - cancel_job → should not occur (cleared on startup), but if it does, clear to NULL
   - NULL → proceed to next steps

2. Clear stale exclusive jobs:
   - If cancel_state is NULL but archive_job or reset_sent_job is
     "starting" or "in_progress", the job crashed without cleaning up.
   - Set job status to "error" and log the event.
   - This prevents stale spinners on the dashboard.

3. Check for stalled scans (in_progress + last_fetched_at > 2min)
   → Reset to error status

4. Auto-clear error status when unlabeled count is 0

5. Check for unlabeled messages + scan not running
   → Retrigger scan chain (sent → relabel → label)
```

## Stale state detection

Every status that can get stuck at "in_progress" has a detection and recovery mechanism:

| What | Detection | Recovery | Where |
|---|---|---|---|
| Inbox scan stuck in_progress | `last_fetched_at` > 2min old | Reset to `error.scan.stalled` | `/api/me` step 3 |
| Inbox scan error + no unlabeled | Error status + unlabeled count = 0 | Clear to `complete` | `/api/me` step 4 |
| Archive job stuck in_progress | `cancel_state` is NULL but job is in_progress | Set to `error` | `/api/me` step 2 |
| Reset sent job stuck in_progress | `cancel_state` is NULL but job is in_progress | Set to `error` | `/api/me` step 2 |
| Sent scan stuck in_progress | Scan exits via `should_continue` | Scan sets own status to `cancelled` | `_run_sent_scan` |
| `cancel_job` after crash | `cancel_state = cancel_job` on boot | Clear to NULL | Lifespan startup |

The principle: if `cancel_state` is NULL, no exclusive job should be running. Any job status claiming "in_progress" in that state is stale and gets cleared to "error".

Scan loops always set their own exit status (`cancelled`, `complete`, or `error`) — they never leave "in_progress" behind. This was a prior bug that caused phantom spinners.

Step 1 blocks step 4 — scan loops never start while cancel_state is non-NULL.

## Row locking

`try_lock_user_scan()` uses `SELECT ... FOR UPDATE SKIP LOCKED` on `scan_state`. This prevents two processes from running the same user's scans concurrently. If the lock is taken, the second caller skips.

The cancel state is separate from the row lock — it's a cooperative signal. The lock prevents concurrent execution; the cancel state requests graceful exit.

## Fault tolerance

### Scan loop crash
- Status stuck at `in_progress` → stalled detection resets to error → retrigger resumes
- Per-batch fresh DB connections prevent Neon idle timeout
- Labels applied to Gmail are permanent; DB progress may lag but catches up on retry

### Exclusive job crash
- Job status stuck at `in_progress` in DB
- `cancel_state` stays at `cancel_scans` → scan loops blocked
- On restart: lifespan preserves `cancel_scans` state
- `/api/me` detects incomplete job and resumes it
- Job is idempotent — restarting from scratch is safe

### Exclusive job crash after user cancelled
- `cancel_state` is `cancel_job`
- On restart: lifespan clears to `NULL`
- Scans can retrigger normally
- Incomplete job state stays in DB but is not resumed (user wanted it stopped)

### Server crash
- Lifespan startup clears `cancel_job` flags, preserves `cancel_scans` flags
- Thread registry cleared (in-memory)
- `/api/me` priority order detects incomplete jobs and resumes them
- No manual intervention required

## Global vs per-user cancellation

| Scope | Mechanism | When |
|---|---|---|
| Global (all users) | `_shutdown_event` (in-memory threading.Event) | SIGTERM, SIGINT, lifespan shutdown |
| Per-user | `cancel_state` (DB column) | Exclusive jobs, disconnect, pause |

Both are checked by the respective `should_continue` functions. Global takes precedence (checked first).
