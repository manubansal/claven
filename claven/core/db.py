"""Database access layer — all reads and writes go through this module.

All functions take an open psycopg2 connection as their first argument.
Transaction management (commit / rollback) is the caller's responsibility;
use get_connection() for production code or inject a test connection directly.
"""

import logging
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


@contextmanager
def get_connection(database_url: str | None = None):
    """Open a DB connection, commit on success, rollback on exception."""
    url = database_url or os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    psycopg2.extras.register_uuid(conn_or_curs=conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(conn, email: str) -> str:
    """Insert user if not present; return user_id as str."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (email)
            VALUES (%s)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
            """,
            (email,),
        )
        return str(cur.fetchone()[0])


def get_user_by_email(conn, email: str) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, email, created_at FROM users WHERE email = %s",
            (email,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_id(conn, user_id: str) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id::text, email, created_at FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_all_users(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id::text, email, created_at FROM users")
        return [dict(row) for row in cur.fetchall()]


# ── Gmail tokens ──────────────────────────────────────────────────────────────

def store_tokens(
    conn,
    user_id: str,
    access_token_enc: bytes,
    refresh_token_enc: bytes,
    token_expiry,
    scopes: list[str],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO gmail_tokens (user_id, access_token_enc, refresh_token_enc, token_expiry, scopes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token_enc = EXCLUDED.access_token_enc,
                refresh_token_enc = EXCLUDED.refresh_token_enc,
                token_expiry = EXCLUDED.token_expiry,
                scopes = EXCLUDED.scopes
            """,
            (user_id, access_token_enc, refresh_token_enc, token_expiry, scopes),
        )


def load_tokens(conn, user_id: str) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT access_token_enc, refresh_token_enc, token_expiry, scopes
            FROM gmail_tokens WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_credentials(conn, user_id: str) -> None:
    """Remove gmail_tokens and scan_state for a user (keeps the users row)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gmail_tokens WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM scan_state WHERE user_id = %s", (user_id,))


def clear_watch_state(conn, user_id: str) -> None:
    """Clear the Gmail watch without losing scan progress.

    Nulls out history_id so polling/webhook processing stops, but keeps
    processed_count, sent scan state, and known senders intact. The user
    can resume filtering with a single click (no re-scan required).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET history_id = 0, updated_at = NOW() WHERE user_id = %s",
            (user_id,),
        )


# ── Scan state ────────────────────────────────────────────────────────────────

def ensure_scan_state(conn, user_id: str) -> None:
    """Create a scan_state row if one doesn't exist yet."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scan_state (user_id, history_id) VALUES (%s, 0) ON CONFLICT DO NOTHING",
            (user_id,),
        )


def try_lock_user_scan(conn, user_id: str) -> bool:
    """Attempt to acquire a row-level lock on the user's scan_state row.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so a concurrent instance that
    already holds the lock gets False immediately (no blocking).  The lock
    is released when the transaction commits or rolls back.

    Creates the scan_state row if it doesn't exist yet.

    Returns True if the lock was acquired, False if another process holds it.
    """
    ensure_scan_state(conn, user_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM scan_state WHERE user_id = %s FOR UPDATE SKIP LOCKED",
            (user_id,),
        )
        return cur.fetchone() is not None


def get_history_id(conn, user_id: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT history_id FROM scan_state WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def set_history_id(conn, user_id: str, history_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scan_state (user_id, history_id)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                history_id = EXCLUDED.history_id,
                updated_at = NOW()
            """,
            (user_id, history_id),
        )


def touch_last_labeled(conn, user_id: str) -> None:
    """Set last_labeled_at to NOW()."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET last_labeled_at = NOW() WHERE user_id = %s",
            (user_id,),
        )


def get_last_labeled_at(conn, user_id: str):
    """Return last_labeled_at timestamp or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_labeled_at FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def touch_last_fetched(conn, user_id: str) -> None:
    """Set last_fetched_at to NOW()."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET last_fetched_at = NOW() WHERE user_id = %s",
            (user_id,),
        )


def get_last_fetched_at(conn, user_id: str):
    """Return last_fetched_at timestamp or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_fetched_at FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_inbox_scan_status(conn, user_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT inbox_scan_status FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_inbox_scan_status(conn, user_id: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET inbox_scan_status = %s, updated_at = NOW() WHERE user_id = %s",
            (status, user_id),
        )


def update_newest_labeled(conn, user_id: str, message_date_ms: int) -> None:
    """Update newest_labeled_at if the given message date is newer than what's stored."""
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(message_date_ms / 1000, tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scan_state
            SET newest_labeled_at = GREATEST(COALESCE(newest_labeled_at, '1970-01-01'::timestamptz), %s)
            WHERE user_id = %s
            """,
            (dt, user_id),
        )


def get_newest_labeled_at(conn, user_id: str):
    """Return newest_labeled_at timestamp or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT newest_labeled_at FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def is_inbox_scan_completed(conn, user_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT inbox_scan_completed FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False


def set_inbox_scan_completed(conn, user_id: str, completed: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET inbox_scan_completed = %s, updated_at = NOW() WHERE user_id = %s",
            (completed, user_id),
        )


def get_processed_count(conn, user_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT processed_count FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return row[0] if row else 0


def increment_processed_count(conn, user_id: str, n: int) -> None:
    if n <= 0:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET processed_count = processed_count + %s WHERE user_id = %s",
            (n, user_id),
        )


def set_processed_count(conn, user_id: str, n: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET processed_count = %s WHERE user_id = %s",
            (n, user_id),
        )


def get_sent_scan_cursor(conn, user_id: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sent_scan_cursor FROM scan_state WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def set_sent_scan_cursor(conn, user_id: str, cursor: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scan_state (user_id, history_id, sent_scan_cursor)
            VALUES (%s, 0, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                sent_scan_cursor = EXCLUDED.sent_scan_cursor,
                updated_at = NOW()
            """,
            (user_id, cursor),
        )


def get_sent_scan_progress(conn, user_id: str) -> dict:
    """Return sent scan progress: messages_scanned, messages_total, status, updated_at."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sent_messages_scanned, sent_messages_total, sent_scan_status, updated_at FROM scan_state WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            return {"messages_scanned": row[0], "messages_total": row[1], "status": row[2], "updated_at": row[3]}
        return {"messages_scanned": 0, "messages_total": None, "status": None, "updated_at": None}


def set_sent_scan_status(conn, user_id: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scan_state (user_id, history_id, sent_scan_status)
            VALUES (%s, 0, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                sent_scan_status = EXCLUDED.sent_scan_status,
                updated_at = NOW()
            """,
            (user_id, status),
        )


def set_sent_scan_progress(conn, user_id: str, scanned: int, total: int | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scan_state (user_id, history_id, sent_messages_scanned, sent_messages_total)
            VALUES (%s, 0, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                sent_messages_scanned = EXCLUDED.sent_messages_scanned,
                sent_messages_total = EXCLUDED.sent_messages_total,
                updated_at = NOW()
            """,
            (user_id, scanned, total),
        )


# ── Sent recipients ───────────────────────────────────────────────────────────

def count_known_senders(conn, user_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sent_recipients WHERE user_id = %s", (user_id,))
        return cur.fetchone()[0]


def get_known_senders(conn, user_id: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM sent_recipients WHERE user_id = %s", (user_id,))
        return {row[0] for row in cur.fetchall()}


def add_known_sender(conn, user_id: str, email_addr: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sent_recipients (user_id, email)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (user_id, email_addr),
        )


def bulk_add_known_senders(conn, user_id: str, email_addrs: list[str]) -> None:
    """Insert known senders. New senders get relabel_status='pending'."""
    if not email_addrs:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO sent_recipients (user_id, email, relabel_status)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            [(user_id, addr, "pending") for addr in email_addrs],
        )


def get_pending_relabel_senders(conn, user_id: str) -> list[str]:
    """Return email addresses that need relabeling."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email FROM sent_recipients WHERE user_id = %s AND relabel_status = 'pending'",
            (user_id,),
        )
        return [row[0] for row in cur.fetchall()]


def mark_relabel_done(conn, user_id: str, email_addrs: list[str]) -> None:
    """Mark senders as relabeled."""
    if not email_addrs:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sent_recipients SET relabel_status = 'done' WHERE user_id = %s AND email = ANY(%s)",
            (user_id, email_addrs),
        )


# ── Scan scope ────────────────────────────────────────────────────────────────

def get_scan_scope(conn, user_id: str) -> str:
    """Return scan_scope ('inbox' or 'allmail'). Defaults to 'inbox'."""
    with conn.cursor() as cur:
        cur.execute("SELECT scan_scope FROM scan_state WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else "inbox"


def set_scan_scope(conn, user_id: str, scope: str) -> None:
    """Set scan_scope to 'inbox' or 'allmail'."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET scan_scope = %s, updated_at = NOW() WHERE user_id = %s",
            (scope, user_id),
        )


# ── Inbox mode (label_only / gatekeeper / sunset) ──────────────────────────

def get_inbox_mode(conn, user_id: str) -> dict:
    """Return {mode, sunset_hours, changed_at}. No scan_state row → label_only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT inbox_mode, sunset_hours, inbox_mode_changed_at FROM scan_state WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        logger.debug("get_inbox_mode: no scan_state for %s, defaulting to label_only", user_id)
        return {"mode": "label_only", "sunset_hours": 24, "changed_at": None}
    logger.debug("get_inbox_mode: %s mode=%s hours=%s", user_id, row[0], row[1])
    return {"mode": row[0], "sunset_hours": row[1], "changed_at": row[2]}


def set_inbox_mode(conn, user_id: str, mode: str, sunset_hours: int | None = None) -> None:
    """Set inbox_mode (and optionally the Sunset timer).

    inbox_mode_changed_at moves only when the mode actually changes, so
    adjusting the timer does not restart it. auto_archive_unknown is kept in
    sync for rollback compatibility.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scan_state SET
                inbox_mode_changed_at = CASE WHEN inbox_mode IS DISTINCT FROM %(mode)s
                                             THEN NOW() ELSE inbox_mode_changed_at END,
                inbox_mode = %(mode)s,
                sunset_hours = COALESCE(%(hours)s, sunset_hours),
                auto_archive_unknown = (%(mode)s = 'gatekeeper'),
                updated_at = NOW()
            WHERE user_id = %(user_id)s
            """,
            {"mode": mode, "hours": sunset_hours, "user_id": user_id},
        )


def get_sunset_users(conn) -> list[dict]:
    """Return [{user_id, sunset_hours, changed_at}] for users in Sunset mode."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id::text, sunset_hours, inbox_mode_changed_at FROM scan_state WHERE inbox_mode = 'sunset'"
        )
        return [{"user_id": r[0], "sunset_hours": r[1], "changed_at": r[2]} for r in cur.fetchall()]


def get_auto_archive_unknown(conn, user_id: str) -> bool:
    """True when unknown-sender mail is archived immediately (Gatekeeper mode)."""
    return get_inbox_mode(conn, user_id)["mode"] == "gatekeeper"


def set_auto_archive_unknown(conn, user_id: str, enabled: bool) -> None:
    """Legacy on/off setting: on = Gatekeeper, off = Label only."""
    set_inbox_mode(conn, user_id, "gatekeeper" if enabled else "label_only")


# ── Event log ─────────────────────────────────────────────────────────────────

def log_event(conn, user_id: str, event_type: str, message: str) -> None:
    """Write an event to the event log."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO event_log (user_id, event_type, message) VALUES (%s, %s, %s)",
            (user_id, event_type, message),
        )


def get_recent_events(conn, user_id: str, limit: int = 30) -> list[dict]:
    """Return the most recent events for a user, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, event_type, message FROM event_log WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s",
            (user_id, limit),
        )
        return [
            {"timestamp": row[0].isoformat(), "event_type": row[1], "message": row[2]}
            for row in cur.fetchall()
        ]


def trim_event_log(conn, user_id: str, keep: int = 100) -> None:
    """Delete old events beyond the keep limit."""
    with conn.cursor() as cur:
        cur.execute(
            """DELETE FROM event_log WHERE user_id = %s AND id NOT IN (
                SELECT id FROM event_log WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s
            )""",
            (user_id, user_id, keep),
        )


# ── Cancel state machine ──────────────────────────────────────────────────────
# States: NULL (clean), "cancel_scans" (exclusive job pending), "cancel_job" (user cancelled)

def get_cancel_state(conn, user_id: str) -> str | None:
    """Return cancel_state or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT cancel_state FROM scan_state WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_cancel_state(conn, user_id: str, state: str) -> None:
    """Set cancel_state to 'cancel_scans' or 'cancel_job'."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET cancel_state = %s WHERE user_id = %s",
            (state, user_id),
        )


def clear_cancel_state(conn, user_id: str) -> None:
    """Clear cancel_state to NULL."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scan_state SET cancel_state = NULL WHERE user_id = %s",
            (user_id,),
        )


def clear_cancel_job_flags(conn) -> None:
    """Clear cancel_job flags on startup. Preserve cancel_scans (job pending)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE scan_state SET cancel_state = NULL WHERE cancel_state = 'cancel_job'")


# ── Archive job state ────────────────────────────────────────────────────────

def set_archive_job(conn, user_id: str, job_id: str, status: str,
                    total: int | None = None, progress: int = 0) -> None:
    """Create or update archive job state."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE scan_state SET
                archive_job_id = %s,
                archive_job_status = %s,
                archive_job_total = %s,
                archive_job_progress = %s,
                updated_at = NOW()
            WHERE user_id = %s""",
            (job_id, status, total, progress, user_id),
        )


def get_archive_job(conn, user_id: str) -> dict | None:
    """Return archive job state or None."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT archive_job_id, archive_job_status,
                      archive_job_total, archive_job_progress
            FROM scan_state WHERE user_id = %s""",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        return {
            "job_id": row[0],
            "status": row[1],
            "total": row[2],
            "progress": row[3],
        }


def clear_archive_job(conn, user_id: str) -> None:
    """Clear archive job state."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE scan_state SET
                archive_job_id = NULL,
                archive_job_status = NULL,
                archive_job_total = NULL,
                archive_job_progress = NULL
            WHERE user_id = %s""",
            (user_id,),
        )


# ── Reset sent scan job state ────────────────────────────────────────────���───

def set_reset_sent_job(conn, user_id: str, job_id: str, status: str,
                       total: int | None = None, progress: int = 0) -> None:
    """Create or update reset sent scan job state."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE scan_state SET
                reset_sent_job_id = %s,
                reset_sent_job_status = %s,
                reset_sent_job_total = %s,
                reset_sent_job_progress = %s,
                updated_at = NOW()
            WHERE user_id = %s""",
            (job_id, status, total, progress, user_id),
        )


def get_reset_sent_job(conn, user_id: str) -> dict | None:
    """Return reset sent scan job state or None."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT reset_sent_job_id, reset_sent_job_status,
                      reset_sent_job_total, reset_sent_job_progress
            FROM scan_state WHERE user_id = %s""",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        return {
            "job_id": row[0],
            "status": row[1],
            "total": row[2],
            "progress": row[3],
        }
