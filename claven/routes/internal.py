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
    COUNTDOWN_LABELS,
    archive_expired_unknown,
    clear_countdowns,
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
    _require_internal_auth(request)

    label_configs = _srv.load_config().get("labels", [])
    results = []

    with _srv.db.get_connection() as conn:
        users = _srv.db.get_all_users(conn)

    for user in users:
        user_id = user["id"]
        with _srv.db.get_connection() as conn:
            try:
                if not _srv.db.try_lock_user_scan(conn, user_id):
                    results.append({"user_id": user_id, "status": "skipped", "detail": "locked"})
                    continue

                history_id = _srv.db.get_history_id(conn, user_id)
                if not history_id:
                    continue

                service = _srv.auth.get_service(conn, user_id, os.environ["TOKEN_ENCRYPTION_KEY"])

                # Reconcile: if sent scan never completed, run it now
                scan_progress = _srv.db.get_sent_scan_progress(conn, user_id)
                logger.debug("internal_poll: user %s sent_scan status=%s", user_id, scan_progress["status"])
                if _srv._needs_sent_scan(scan_progress):
                    logger.info("Reconciling sent scan for user %s (status=%s)", user_id, scan_progress["status"])
                    _srv.db.set_sent_scan_status(conn, user_id, "in_progress")
                    try:
                        _srv.build_known_senders(service, conn, user_id)
                        _srv.db.set_sent_scan_status(conn, user_id, "complete")
                    except Exception as exc:
                        from claven.tasks import _handle_error
                        error_label = _handle_error(exc, user_id, "Sent scan reconciliation", conn)
                        _srv.db.set_sent_scan_status(conn, user_id, error_label)

                known_senders = _srv.db.get_known_senders(conn, user_id)
                auto_archive = _srv.db.get_auto_archive_unknown(conn, user_id)

                profile = _srv.get_profile(service)
                latest_history_id = int(profile["historyId"])

                label_id_cache = _srv._label_id_cache_for_config(service, label_configs)
                sunset = _srv._sunset_for_live_labeling(service, conn, user_id)
                count = _srv.poll_new_messages(service, history_id, label_configs, label_id_cache, known_senders,
                                               auto_archive_unknown=auto_archive, sunset=sunset)
                _srv.db.touch_last_fetched(conn, user_id)
                _srv.db.set_history_id(conn, user_id, latest_history_id)
                if count is not None and count > 0:
                    _srv.db.increment_processed_count(conn, user_id, count)
                    _srv.db.touch_last_labeled(conn, user_id)
                results.append({"user_id": user_id, "status": "ok"})
            except Exception as exc:
                from claven.tasks import _handle_error
                error_label = _handle_error(exc, user_id, "Poll processing", conn)
                results.append({"user_id": user_id, "status": "error", "detail": error_label})

    return {"processed": len(results), "results": results}


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
    than the user's timer is archived, and every message still waiting gets a
    countdown label (c/4h, ...). For users who left Sunset, the
    countdown labels are deleted. Idempotent. Intended to be triggered every
    15 minutes by Cloud Scheduler.
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
                deleted = clear_countdowns(service)
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
                label_ids = build_label_id_cache(service, [unknown_label, *COUNTDOWN_LABELS])
                archived = 0
                if cutoff is None:
                    logger.debug("sunset: %s timer not yet run since switching, nothing to archive", user_id)
                else:
                    archived = archive_expired_unknown(service, unknown_label, cutoff)
                if archived:
                    logger.debug("sunset: %s archived %d", user_id, archived)
                    _srv.db.log_event(conn, user_id, "sunset_archived",
                                      f"Sunset archived {archived} unread unknown-sender message{'s' if archived != 1 else ''}")
                else:
                    logger.debug("sunset: %s nothing archived", user_id)
                countdown_ids = {name: label_ids[name] for name in COUNTDOWN_LABELS}
                update_countdowns(service, unknown_label, now, user["sunset_hours"], user["changed_at"], countdown_ids)
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

    notification_history_id = int(history_id_str)
    label_configs = _srv.load_config().get("labels", [])

    with _srv.db.get_connection() as conn:
        user = _srv.db.get_user_by_email(conn, email)
        if not user:
            logger.info("Webhook for unknown user %s — acknowledging", email)
            return {"status": "ok", "detail": "unknown user"}

        if not _srv.db.try_lock_user_scan(conn, user["id"]):
            logger.info("Webhook for %s skipped — locked by another instance", email)
            return {"status": "ok", "detail": "locked"}

        stored_history_id = _srv.db.get_history_id(conn, user["id"])
        if not stored_history_id:
            logger.info("No history_id for %s — skipping", email)
            return {"status": "ok", "detail": "no history_id"}

        logger.debug("webhook_gmail: processing for %s (history_id=%d)", email, stored_history_id)
        try:
            service = _srv.auth.get_service(conn, user["id"], os.environ["TOKEN_ENCRYPTION_KEY"])

            # Incremental known senders update — cheap with cursor (one list_history call).
            # Also serves as reconciliation: if the initial scan never completed,
            # build_known_senders falls through to a full scan.
            scan_progress = _srv.db.get_sent_scan_progress(conn, user["id"])
            if _srv._needs_sent_scan(scan_progress):
                logger.debug("webhook_gmail: reconciling sent scan (status=%s)", scan_progress["status"])
                _srv.db.set_sent_scan_status(conn, user["id"], "in_progress")
            _srv.build_known_senders(service, conn, user["id"])
            if scan_progress["status"] != "complete":
                _srv.db.set_sent_scan_status(conn, user["id"], "complete")

            known_senders = _srv.db.get_known_senders(conn, user["id"])
            auto_archive = _srv.db.get_auto_archive_unknown(conn, user["id"])
            label_id_cache = _srv._label_id_cache_for_config(service, label_configs)
            sunset = _srv._sunset_for_live_labeling(service, conn, user["id"])
            count = _srv.poll_new_messages(service, stored_history_id, label_configs, label_id_cache, known_senders,
                                           auto_archive_unknown=auto_archive, sunset=sunset)
            _srv.db.touch_last_fetched(conn, user["id"])
            _srv.db.set_history_id(conn, user["id"], notification_history_id)
            if count is not None and count > 0:
                _srv.db.increment_processed_count(conn, user["id"], count)
                _srv.db.touch_last_labeled(conn, user["id"])
        except Exception as exc:
            from claven.tasks import _handle_error
            _handle_error(exc, user["id"], "Webhook", conn)

    return {"status": "ok"}
