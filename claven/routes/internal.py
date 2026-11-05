"""Internal + webhook routes (/internal/poll, /internal/renew-watches, /internal/build-known-senders, /internal/sunset, /webhook/gmail)."""

import base64
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

import claven.server as _srv
from claven.core.gmail import build_label_id_cache
from claven.core.labels import UNKNOWN
from claven.core.sunset import (
    archive_expired_unknown,
    clear_countdowns,
    countdown_labels_for,
    existing_countdown_ids,
    sunset_cutoff,
    update_countdowns,
)
from claven.routes.auth import _require_internal_auth, _verify_pubsub_token

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/internal/renew-watches")
def internal_renew_watches(request: Request):
    """Renew Gmail push watches for all connected users.

    Calls start_watch() for each user with an active history_id.
    Idempotent — extends existing watches or creates new ones.
    Intended to be triggered daily by Cloud Scheduler.
    """
    _require_internal_auth(request)
    results = []

    with _srv.db.get_connection() as conn:
        users = _srv.db.get_all_users(conn)

    for user in users:
        user_id = user["id"]
        with _srv.db.get_connection() as conn:
            try:
                history_id = _srv.db.get_history_id(conn, user_id)
                if not history_id:
                    logger.debug("renew-watches: skipping %s (not connected)", user_id)
                    continue
                service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])
                _srv.start_watch(service, os.environ["PUBSUB_TOPIC"])
                results.append({"user_id": user_id, "status": "ok"})
                logger.info("Renewed watch for %s", user_id)
            except Exception as exc:
                from claven.tasks import _handle_error
                _handle_error(exc, user_id, "Watch renewal", conn)
                results.append({"user_id": user_id, "status": "error", "detail": str(exc)})

    return {"renewed": len([r for r in results if r["status"] == "ok"]), "results": results}


@router.post("/internal/poll")
def internal_poll(request: Request):
    """Label new mail for every user. Cloud Scheduler calls this every 20 minutes.

    Gmail pushes only get acknowledged (see webhook_gmail): handling each one
    woke the Neon database, which bills while awake, hundreds of times a day.
    """
    _require_internal_auth(request)

    label_configs = _srv.load_config().get("labels", [])
    results = []

    with _srv.db.get_connection() as conn:
        users = _srv.db.get_all_users(conn)

    for user in users:
        with _srv.db.get_connection() as conn:
            try:
                result = _process_user(conn, user, label_configs)
            except Exception as exc:
                from claven.tasks import _handle_error
                logger.debug("internal_poll: processing failed for %s", user["id"])
                result = {"status": "error", "detail": _handle_error(exc, user["id"], "Poll processing", conn)}
        results.append({"user_id": user["id"], **result})

    return {"processed": len(results), "results": results}


def _process_user(conn, user: dict, label_configs: list) -> dict:
    """Label one user's mail that arrived since their stored history id.

    Returns {"status": ..., "detail": ...} saying what happened.
    """
    user_id = user["id"]
    if not _srv.db.try_lock_user_scan(conn, user_id):
        logger.info("Poll for %s skipped — locked by another instance", user_id)
        return {"status": "skipped", "detail": "locked"}

    stored_history_id = _srv.db.get_history_id(conn, user_id)
    if not stored_history_id:
        logger.debug("_process_user: no history_id for %s", user_id)
        return {"status": "ok", "detail": "no history_id"}

    # While a scan runs, every label it applies adds history. Leave the
    # history id alone: the label scan covers new inbox mail and sets the
    # history id when it finishes. A full sent scan never runs here — inside
    # this transaction it would hold the user's rows locked for minutes.
    scan_progress = _srv.db.get_sent_scan_progress(conn, user_id)
    if scan_progress["status"] != "complete":
        if _srv._needs_sent_scan(scan_progress):
            logger.info("Poll for %s: sent scan unfinished (status=%s), restarting it",
                        user_id, scan_progress["status"])
            _srv.db.set_sent_scan_status(conn, user_id, "in_progress")
            # Release this transaction's scan_state lock first, or the scan's
            # SKIP LOCKED check sees it and skips itself.
            conn.commit()
            _srv._spawn_scan_thread(_srv._run_sent_scan, (user_id,))
            return {"status": "ok", "detail": "sent scan started"}
        logger.debug("_process_user: sent scan running for %s, skipping", user_id)
        return {"status": "ok", "detail": "sent scan running"}
    if _srv._inbox_scan_running(_srv.db.get_inbox_scan_status(conn, user_id),
                                _srv.db.get_last_fetched_at(conn, user_id)):
        logger.debug("_process_user: label scan running for %s, skipping", user_id)
        return {"status": "ok", "detail": "inbox scan running"}

    logger.debug("_process_user: processing for %s (history_id=%d)", user_id, stored_history_id)
    service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])
    # Read before labeling: mail arriving meanwhile has a later id and is
    # picked up next run (labeling is idempotent).
    latest_history_id = int(_srv.get_profile(service)["historyId"])

    # Label changes (including Claven's own) add history too; skip the work
    # unless a message was added.
    if not _srv.has_new_messages(service, stored_history_id):
        logger.debug("_process_user: no new messages for %s, advancing history only", user_id)
        _srv.db.set_history_id(conn, user_id, latest_history_id)
        return {"status": "ok", "detail": "no new messages"}
    # Incremental known senders update — cheap with cursor (one list_history call).
    _srv.build_known_senders(service, conn, user_id)

    known_senders = _srv.db.get_known_senders(conn, user_id)
    auto_archive = _srv.db.get_auto_archive_unknown(conn, user_id)
    label_id_cache = _srv._label_id_cache_for_config(service, label_configs)
    sunset = _srv._sunset_for_live_labeling(service, conn, user_id)
    count = _srv.poll_new_messages(service, stored_history_id, label_configs, label_id_cache, known_senders,
                                   auto_archive_unknown=auto_archive, sunset=sunset)
    _srv.db.touch_last_fetched(conn, user_id)
    _srv.db.set_history_id(conn, user_id, latest_history_id)
    if count is not None and count > 0:
        logger.debug("_process_user: labeled %d for %s", count, user_id)
        _srv.db.increment_processed_count(conn, user_id, count)
        _srv.db.touch_last_labeled(conn, user_id)
    else:
        logger.debug("_process_user: nothing labeled for %s", user_id)
    # Mail you just sent made its recipients known: move their earlier
    # unknown-sender mail to known now rather than on the next dashboard
    # visit. Runs after new mail is labeled so a failure can't hold that up;
    # senders stay pending and are retried on the next run or visit.
    if _srv.db.get_pending_relabel_senders(conn, user_id):
        logger.debug("_process_user: relabeling newly known senders for %s", user_id)
        _srv.relabel_scan(service, user_id, label_configs, label_id_cache,
                          shutdown_event=_srv._shutdown_event,
                          countdown_ids=existing_countdown_ids(service))
    else:
        logger.debug("_process_user: no newly known senders for %s", user_id)
    return {"status": "ok", "detail": "processed"}


@router.post("/internal/build-known-senders")
def internal_build_known_senders(request: Request):
    """Build or update the known senders list for all connected users.

    Scans each user's Sent mail and populates their sent_recipients rows.
    Uses a per-user cursor (sent_scan_cursor) for incremental updates —
    only new sent messages are processed on subsequent runs.

    Intended to be triggered by a Cloud Scheduler job or by /api/connect.
    """
    _require_internal_auth(request)
    results = []

    with _srv.db.get_connection() as conn:
        users = _srv.db.get_all_users(conn)

    for user in users:
        user_id = user["id"]
        with _srv.db.get_connection() as conn:
            try:
                service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])
                result = _srv.build_known_senders(service, conn, user_id)
                results.append({"user_id": user_id, "status": "ok", **result})
            except Exception as exc:
                from claven.tasks import _handle_error
                error_label = _handle_error(exc, user_id, "Known senders scan", conn)
                results.append({"user_id": user_id, "status": "error", "detail": error_label})

    return {"processed": len(results), "results": results}


@router.post("/internal/sunset")
def internal_sunset(request: Request):
    """Sunset sweep: archive expired unknown-sender mail and update countdown labels.

    For users in Sunset mode, unread, unstarred unknown-sender inbox mail older
    than the user's timer is archived, and every message still waiting carries a
    countdown label (c/4h, ...) in place of the unknown label. For users who left
    Sunset, the countdown labels are deleted and their mail goes back to the
    unknown label. Idempotent. Triggered hourly by Cloud Scheduler, alongside
    /internal/poll so both share one database wake.
    """
    _require_internal_auth(request)
    now = datetime.now(timezone.utc)
    label_configs = _srv.load_config().get("labels", [])
    unknown_label = label_configs[0].get("unknown_label", UNKNOWN) if label_configs else UNKNOWN
    archived_total = 0
    results = []

    with _srv.db.get_connection() as conn:
        cleanup_users = _srv.db.get_countdown_cleanup_users(conn)
        users = _srv.db.get_sunset_users(conn)

    for user_id in cleanup_users:
        with _srv.db.get_connection() as conn:
            try:
                service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])
                unknown_label_id = build_label_id_cache(service, [unknown_label])[unknown_label]
                deleted = clear_countdowns(service, unknown_label_id)
                _srv.db.set_sunset_countdown_active(conn, user_id, False)
                logger.debug("sunset: %s left Sunset, deleted %d countdown labels", user_id, deleted)
            except Exception as exc:
                from claven.tasks import _handle_error
                _handle_error(exc, user_id, "Sunset cleanup", conn)

    for user in users:
        user_id = user["user_id"]
        cutoff = sunset_cutoff(now, user["sunset_hours"], user["changed_at"])
        with _srv.db.get_connection() as conn:
            try:
                if not _srv.db.get_history_id(conn, user_id):
                    logger.debug("sunset: skipping %s (not connected)", user_id)
                    continue
                service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])
                # Builds (and renames old) labels before any search by label name.
                needed = countdown_labels_for(user["sunset_hours"])
                label_ids = build_label_id_cache(service, [unknown_label, *needed])
                # Labels for hours beyond the timer may still be on mail from a longer timer.
                countdown_ids = {**existing_countdown_ids(service), **{name: label_ids[name] for name in needed}}
                archived = 0
                if cutoff is None:
                    logger.debug("sunset: %s timer not yet run since switching, nothing to archive", user_id)
                else:
                    archived = archive_expired_unknown(service, unknown_label, label_ids[unknown_label], cutoff,
                                                       countdown_ids)
                if archived:
                    logger.debug("sunset: %s archived %d", user_id, archived)
                    _srv.db.log_event(conn, user_id, "sunset_archived",
                                      f"Sunset archived {archived} unread unknown-sender message{'s' if archived != 1 else ''}")
                else:
                    logger.debug("sunset: %s nothing archived", user_id)
                update_countdowns(service, unknown_label, label_ids[unknown_label], now,
                                  user["sunset_hours"], user["changed_at"], countdown_ids)
                # Mail has just moved off any hours beyond the timer; drop those labels.
                if countdown_ids.keys() - set(needed):
                    deleted = clear_countdowns(service, label_ids[unknown_label], keep=needed)
                    logger.info("Sunset deleted %d countdown labels beyond the %dh timer for %s",
                                deleted, user["sunset_hours"], user_id)
                else:
                    logger.debug("sunset: %s has no countdown labels beyond the timer", user_id)
                _srv.db.set_sunset_countdown_active(conn, user_id, True)
                archived_total += archived
                results.append({"user_id": user_id, "status": "ok", "archived": archived})
            except Exception as exc:
                from claven.tasks import _handle_error
                _handle_error(exc, user_id, "Sunset", conn)
                results.append({"user_id": user_id, "status": "error", "detail": str(exc)})

    return {"archived": archived_total, "results": results}


@router.post("/webhook/gmail")
async def webhook_gmail(request: Request):
    _verify_pubsub_token(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    message = body.get("message")
    if not message:
        raise HTTPException(status_code=400, detail="Missing 'message' field")

    try:
        data = base64.b64decode(message.get("data", ""))
        notification = json.loads(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed message data")

    email = notification.get("emailAddress")
    history_id_str = notification.get("historyId")
    if not email or not history_id_str:
        raise HTTPException(status_code=400, detail="Missing emailAddress or historyId")

    # Acknowledge only. Handling a push needs the database, and each one woke
    # Neon for at least five minutes; internal_poll labels the new mail within
    # 20 minutes instead. The Gmail watch stays so pushes can be turned back on.
    logger.debug("webhook_gmail: push for history %s acknowledged, left to the poll", history_id_str)
    return {"status": "ok", "detail": "deferred to poll"}
