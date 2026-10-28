"""Background task runners and helpers.

All background tasks (scans and jobs) go through _run_task, which provides:
- Row-level locking (try_lock_user_scan)
- Status lifecycle (in_progress → complete/cancelled/error)
- Error classification (_classify_error → health code labels)
- Event logging (never raw exceptions in activity log)
- Debug logging at every branch point
- Optional thread waiting (for exclusive jobs)
- Optional cleanup (for exclusive jobs)
- Optional chaining (sent scan → relabel → inbox)
"""

import logging
import os
import threading
import time as _time
from datetime import datetime, timezone, timedelta

import claven.server as _srv
from claven.core.sunset import COUNTDOWN_LABELS, countdown_labels_for, existing_countdown_ids

logger = logging.getLogger(__name__)

_STALE_SCAN_THRESHOLD = timedelta(minutes=1)


# ── Error classification ─────────────────────────────────────────────────────

def _classify_error(exc: Exception) -> str:
    """Map an exception to a health code label.

    Examines the exception string to classify it into a known error category.
    Used by _run_task to store classified labels instead of raw exception text.
    """
    exc_str = str(exc).lower()
    if "connection" in exc_str or "closed" in exc_str or "ssl" in exc_str:
        return "error.db.connection_lost"
    if "429" in exc_str or "rate" in exc_str:
        return "error.gmail.rate_limited"
    if "401" in exc_str or "403" in exc_str or "token" in exc_str or "invalid_grant" in exc_str:
        return "error.gmail.auth_expired"
    if "quota" in exc_str:
        return "error.gmail.quota_exhausted"
    if "HttpError" in type(exc).__name__ or "gmail" in exc_str:
        return "error.gmail.api"
    return "error.unknown"


def _handle_error(exc: Exception, user_id: str, context: str, conn=None) -> str:
    """Classify an error, log it, and write to the event log.

    Returns the classified error label for callers that need to set status.
    conn is optional — if None or DB write fails, event log is skipped.
    """
    error_label = _classify_error(exc)
    logger.exception("%s failed for %s: %s (classified: %s)", context, user_id, exc, error_label)
    if conn:
        try:
            _srv.db.log_event(conn, user_id, "error", f"{context} — {error_label}")
        except Exception:
            logger.debug("_handle_error: failed to write event log for %s", user_id)
    return error_label


# ── Helpers ──────────────────────────────────────────────────────────────────

def _needs_sent_scan(scan_progress: dict) -> bool:
    """Return True if the sent scan should be (re-)triggered."""
    status = scan_progress["status"]
    if status == "complete":
        logger.debug("_needs_sent_scan: False (status=complete)")
        return False
    if status == "in_progress":
        updated_at = scan_progress.get("updated_at")
        if updated_at and datetime.now(timezone.utc) - updated_at < _STALE_SCAN_THRESHOLD:
            logger.debug("_needs_sent_scan: False (in_progress, updated_at=%s, not stale)", updated_at)
            return False
        logger.debug("_needs_sent_scan: True (stale in_progress, updated_at=%s)", scan_progress.get("updated_at"))
        return True
    logger.debug("_needs_sent_scan: True (status=%s)", status)
    return True


def _label_id_cache_for_config(service, label_configs: list[dict]) -> dict[str, str]:
    """Build a Gmail label ID cache for all labels referenced in the config."""
    names = []
    for lc in label_configs:
        names.append(lc["id"])
        if unknown := lc.get("unknown_label"):
            names.append(unknown)
    return _srv.build_label_id_cache(service, names)


def _sunset_for_live_labeling(service, conn, user_id: str) -> dict | None:
    """In Sunset mode, the timer and countdown label ids live labeling needs; else None."""
    inbox_mode = _srv.db.get_inbox_mode(conn, user_id)
    if inbox_mode["mode"] != "sunset":
        logger.debug("_sunset_for_live_labeling: %s in %s mode, no countdowns", user_id, inbox_mode["mode"])
        return None
    label_ids = _srv.build_label_id_cache(service, list(countdown_labels_for(inbox_mode["sunset_hours"])))
    # Countdown labels may exist from here on; leaving Sunset must clean them up.
    _srv.db.set_sunset_countdown_active(conn, user_id, True)
    logger.debug("_sunset_for_live_labeling: %s in Sunset (%dh)", user_id, inbox_mode["sunset_hours"])
    return {"hours": inbox_mode["sunset_hours"], "changed_at": inbox_mode["changed_at"], "label_ids": label_ids}


def _is_current_worker() -> bool:
    """Check if this thread should keep running (global check only).

    Returns False if:
    - --reload spawned a new worker (PID changed)
    - Ctrl+C / SIGTERM received (shutdown event set)
    """
    if _srv._shutdown_event.is_set():
        logger.debug("_is_current_worker: False (shutdown_event set)")
        return False
    pid_match = os.getpid() == _srv._worker_id
    if not pid_match:
        logger.debug("_is_current_worker: False (pid=%d, worker_id=%d)", os.getpid(), _srv._worker_id)
    return pid_match


def _cancel_scans_and_wait(user_id: str, timeout: float = 10) -> None:
    """Set cancel_scans state and wait for scan threads to exit."""
    logger.debug("_cancel_scans_and_wait: setting cancel_scans for user %s", user_id)
    with _srv.db.get_connection() as conn:
        _srv.db.set_cancel_state(conn, user_id, "cancel_scans")
    deadline = _time.monotonic() + timeout
    with _srv._threads_lock:
        threads = list(_srv._active_threads)
    logger.debug("_cancel_scans_and_wait: joining %d threads (timeout=%.1f)", len(threads), timeout)
    for t in threads:
        remaining = deadline - _time.monotonic()
        if remaining > 0:
            t.join(timeout=remaining)
    logger.debug("_cancel_scans_and_wait: done waiting")


def _should_continue_scan(user_id: str) -> bool:
    """For scan loops: exit on any non-NULL cancel state."""
    if not _is_current_worker():
        logger.debug("should_continue_scan: _is_current_worker=False (shutdown=%s, pid=%d, worker_id=%d)",
                      _srv._shutdown_event.is_set(), os.getpid(), _srv._worker_id)
        return False
    with _srv.db.get_connection() as conn:
        state = _srv.db.get_cancel_state(conn, user_id)
        if state is not None:
            logger.debug("should_continue_scan: cancel_state=%s", state)
        return state is None


def _should_continue_job(user_id: str) -> bool:
    """For exclusive jobs: exit only on cancel_job."""
    if not _is_current_worker():
        logger.debug("_should_continue_job: False (_is_current_worker=False)")
        return False
    with _srv.db.get_connection() as conn:
        state = _srv.db.get_cancel_state(conn, user_id)
        if state == "cancel_job":
            logger.debug("_should_continue_job: False (cancel_state=cancel_job)")
            return False
        logger.debug("_should_continue_job: True (cancel_state=%s)", state)
        return True


def _cleanup_job(user_id: str) -> None:
    """Clean up after an exclusive job: clear cancel state + resume guard."""
    with _srv.db.get_connection() as conn:
        _srv.db.clear_cancel_state(conn, user_id)
    _srv._resumed_jobs.discard(user_id)


class _TaskCancelled(Exception):
    """Raised inside task_fn when a job detects cancellation mid-batch.

    _run_task catches this specially: it's not an error, it's a clean cancel.
    """
    pass


# ── Unified task runner ──────────────────────────────────────────────────────

def _run_task(
    user_id: str,
    task_name: str,
    task_fn,
    set_status,
    chain: list = None,
    should_continue=None,
    wait_for_threads: bool = False,
    cleanup=None,
):
    """Unified lifecycle for all background tasks (scans and jobs).

    1. Optionally wait for other threads to drain (exclusive jobs)
    2. Acquire row-level lock
    3. Set status to in_progress, log event, get Gmail service
    4. Call task_fn(service) → count
    5. Check cancellation → set cancelled/complete
    6. On exception → classify error, set error status
    7. Cleanup (always, via finally)
    8. Chain to next tasks on success
    """
    if should_continue is None:
        should_continue = lambda: _should_continue_scan(user_id)

    logger.info("%s started for %s (pid=%d)", task_name, user_id, os.getpid())

    # Exclusive jobs register in _resumed_jobs to prevent /api/me from
    # spawning duplicates. The cleanup callback clears this on exit.
    if wait_for_threads:
        _srv._resumed_jobs.add(user_id)

    # Jobs wait for running scans to drain (cancel_scans was set by endpoint)
    if wait_for_threads:
        with _srv._threads_lock:
            threads = [t for t in _srv._active_threads if t is not threading.current_thread()]
        logger.debug("_run_task[%s]: waiting for %d threads", task_name, len(threads))
        for t in threads:
            t.join(timeout=10)
        logger.debug("_run_task[%s]: threads drained", task_name)

    try:
        # Lock + status + service in one short-lived connection
        with _srv.db.get_connection() as conn:
            if not _srv.db.try_lock_user_scan(conn, user_id):
                logger.info("%s skipped for %s — locked by another instance", task_name, user_id)
                return
            set_status(conn, user_id, "in_progress")
            _srv.db.log_event(conn, user_id, "scan", f"{task_name} started")
            service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])
            logger.debug("_run_task[%s]: acquired lock, got service", task_name)

        # Do the actual work
        count = task_fn(service)
        logger.debug("_run_task[%s]: task_fn returned count=%s", task_name, count)

        # Check if cancelled
        if not should_continue():
            logger.debug("_run_task[%s]: cancelled after task_fn", task_name)
            with _srv.db.get_connection() as conn:
                set_status(conn, user_id, "cancelled")
                _srv.db.log_event(conn, user_id, "scan", f"{task_name} cancelled")
            return

        # Success
        with _srv.db.get_connection() as conn:
            set_status(conn, user_id, "complete")
            _srv.db.log_event(conn, user_id, "scan", f"{task_name} complete — {count} processed")
        logger.info("%s complete for %s: %s processed", task_name, user_id, count)

    except _TaskCancelled:
        logger.debug("_run_task[%s]: cancelled mid-task", task_name)
        with _srv.db.get_connection() as conn:
            set_status(conn, user_id, "cancelled")
            _srv.db.log_event(conn, user_id, "scan", f"{task_name} cancelled")
        return

    except Exception as exc:
        try:
            with _srv.db.get_connection() as conn:
                error_label = _handle_error(exc, user_id, task_name, conn)
                set_status(conn, user_id, error_label)
        except Exception:
            logger.exception("Failed to set error status for %s", user_id)
        return

    finally:
        if cleanup:
            cleanup()

    # Chain to next task(s) on success
    for next_task in (chain or []):
        logger.debug("_run_task[%s]: chaining to %s", task_name, next_task.__name__)
        next_task(user_id)


# ── Scan runners ─────────────────────────────────────────────────────────────

def _run_sent_scan(user_id: str):
    """Build known senders list, then chain to relabel → label scan."""
    def task_fn(service):
        return _srv.build_known_senders(
            service, None, user_id,
            should_continue=lambda: _should_continue_scan(user_id),
            shutdown_event=_srv._shutdown_event,
        )
    _run_task(user_id, "Sent scan", task_fn,
              set_status=_srv.db.set_sent_scan_status,
              chain=[_run_relabel_scan, _run_inbox_scan])


def _run_relabel_scan(user_id: str):
    """Relabel messages from newly discovered known senders."""
    def task_fn(service):
        label_configs = _srv.load_config().get("labels", [])
        label_id_cache = _label_id_cache_for_config(service, label_configs)
        return _srv.relabel_scan(
            service, user_id, label_configs, label_id_cache,
            should_continue=lambda: _should_continue_scan(user_id),
            shutdown_event=_srv._shutdown_event,
            countdown_ids=existing_countdown_ids(service),
        )
    _run_task(user_id, "Relabel scan", task_fn,
              set_status=lambda conn, uid, s: None)


def _run_inbox_scan(user_id: str):
    """Scan messages and apply known-sender/unknown-sender labels."""
    def task_fn(service):
        label_configs = _srv.load_config().get("labels", [])
        with _srv.db.get_connection() as conn:
            scope = _srv.db.get_scan_scope(conn, user_id)
            known_senders = _srv.db.get_known_senders(conn, user_id)
            auto_archive = _srv.db.get_auto_archive_unknown(conn, user_id)
        logger.debug("_run_inbox_scan: scope=%s, %d known_senders, auto_archive=%s", scope, len(known_senders), auto_archive)
        label_id_cache = _label_id_cache_for_config(service, label_configs)
        return _srv.scan_inbox(
            service, None, user_id, label_configs, label_id_cache, known_senders,
            should_continue=lambda: _should_continue_scan(user_id),
            shutdown_event=_srv._shutdown_event,
            scope=scope,
            auto_archive_unknown=auto_archive,
            countdown_labels=list(existing_countdown_ids(service)),
        )
    _run_task(user_id, "Label scan", task_fn,
              set_status=_srv.db.set_inbox_scan_status)


# ── Job runners ──────────────────────────────────────────────────────────────

def _run_archive_unknown(user_id: str, job_id: str):
    """Archive all inbox messages with unknown-sender label."""
    def task_fn(service):
        label_configs = _srv.load_config().get("labels", [])
        all_labels = service.users().labels().list(userId="me").execute().get("labels", [])
        label_id_by_name = {l["name"]: l["id"] for l in all_labels}

        unknown_label_id = None
        for lc in label_configs:
            if unknown := lc.get("unknown_label"):
                unknown_label_id = label_id_by_name.get(unknown)
        if not unknown_label_id:
            logger.warning("Archive job %s: no unknown-sender label found", job_id)
            return 0
        # In Sunset, waiting unknown-sender mail carries a countdown label instead.
        marker_ids = [unknown_label_id, *(label_id_by_name[n] for n in COUNTDOWN_LABELS if n in label_id_by_name)]

        # Paginate to get all message IDs (labelIds is AND, so one listing per label)
        all_msg_ids = []
        for marker_id in marker_ids:
            page_token = None
            while True:
                kwargs = {"userId": "me", "labelIds": ["INBOX", marker_id], "maxResults": 500}
                if page_token:
                    kwargs["pageToken"] = page_token
                result = service.users().messages().list(**kwargs).execute()
                all_msg_ids.extend(m["id"] for m in result.get("messages", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    logger.debug("Archive job %s: listed label %s", job_id, marker_id)
                    break

        all_msg_ids = list(dict.fromkeys(all_msg_ids))
        total = len(all_msg_ids)
        from claven.core.scan import _notify_progress
        with _srv.db.get_connection() as conn:
            _srv.db.set_archive_job(conn, user_id, job_id, "in_progress", total=total, progress=0)
            _notify_progress(conn, user_id, "archive_started", job_id=job_id, total=total)
        logger.info("Archive job %s: %d messages to archive", job_id, total)

        if total == 0:
            return 0

        # Archive in batches
        from claven.core.gmail import batch_remove_labels, _BATCH_LIMIT
        archived = 0
        for i in range(0, total, _BATCH_LIMIT):
            if not _should_continue_job(user_id):
                logger.debug("Archive job %s: cancelled at %d/%d", job_id, archived, total)
                raise _TaskCancelled(archived)

            batch_ids = all_msg_ids[i:i + _BATCH_LIMIT]
            modified = batch_remove_labels(service, batch_ids, ["INBOX"])
            archived += modified
            with _srv.db.get_connection() as conn:
                _srv.db.set_archive_job(conn, user_id, job_id, "in_progress", total=total, progress=archived)
                _notify_progress(conn, user_id, "archive_progress", job_id=job_id, total=total, progress=archived)

        return archived

    _run_task(user_id, "Archive", task_fn,
              set_status=lambda conn, uid, s: _srv.db.set_archive_job(conn, uid, job_id, s),
              should_continue=lambda: _should_continue_job(user_id),
              wait_for_threads=True,
              cleanup=lambda: _cleanup_job(user_id))


def _run_reset_sent_scan(user_id: str, job_id: str):
    """Remove the c/s (sent-scanned) label from all sent messages."""
    def task_fn(service):
        from claven.core.scan import SENT_SCANNED_LABEL, _notify_progress
        from claven.core.gmail import gmail_retry, batch_remove_labels, _BATCH_LIMIT

        all_labels = gmail_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
        scanned_label_id = next((l["id"] for l in all_labels if l["name"] == SENT_SCANNED_LABEL), None)
        if not scanned_label_id:
            logger.warning("Reset sent scan job %s: label not found", job_id)
            return 0

        # Paginate to get all message IDs
        all_msg_ids = []
        page_token = None
        while True:
            kw = {"userId": "me", "labelIds": [scanned_label_id], "maxResults": 500}
            if page_token:
                kw["pageToken"] = page_token
            result = gmail_retry(lambda kw=kw: service.users().messages().list(**kw).execute())
            all_msg_ids.extend(m["id"] for m in result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
            _time.sleep(0.5)

        total = len(all_msg_ids)
        with _srv.db.get_connection() as conn:
            _srv.db.set_reset_sent_job(conn, user_id, job_id, "in_progress", total=total, progress=0)
            _notify_progress(conn, user_id, "reset_sent_started", job_id=job_id, total=total)
        logger.info("Reset sent scan job %s: %d messages to reset", job_id, total)

        if total == 0:
            return 0

        # Remove labels in batches
        removed = 0
        for i in range(0, total, _BATCH_LIMIT):
            if not _should_continue_job(user_id):
                logger.debug("Reset sent scan job %s: cancelled at %d/%d", job_id, removed, total)
                raise _TaskCancelled(removed)

            batch_ids = all_msg_ids[i:i + _BATCH_LIMIT]
            modified = batch_remove_labels(service, batch_ids, [scanned_label_id])
            removed += modified
            with _srv.db.get_connection() as conn:
                _srv.db.set_reset_sent_job(conn, user_id, job_id, "in_progress", total=total, progress=removed)
                _notify_progress(conn, user_id, "reset_sent_progress", job_id=job_id, total=total, progress=removed)

        # Reset sent scan status so it retriggers
        with _srv.db.get_connection() as conn:
            _srv.db.set_sent_scan_status(conn, user_id, None)

        return removed

    _run_task(user_id, "Reset sent scan", task_fn,
              set_status=lambda conn, uid, s: _srv.db.set_reset_sent_job(conn, uid, job_id, s),
              should_continue=lambda: _should_continue_job(user_id),
              wait_for_threads=True,
              cleanup=lambda: _cleanup_job(user_id))


