"""Action endpoints (/api/actions/archive-unknown, /api/actions/reset-sent-scan, /api/actions/cancel)."""

import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import claven.server as _srv
from claven.routes.auth import _get_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/actions/reset-sent-scan")
def api_reset_sent_scan(request: Request):
    """Start removing c/s (sent-scanned) labels to force a full re-scan."""
    session = _get_session(request)
    user_id = session["user_id"]
    logger.debug("api_reset_sent_scan: called for user %s", user_id)

    with _srv.db.get_connection() as conn:
        existing = _srv.db.get_reset_sent_job(conn, user_id)
        if existing and existing["status"] == "in_progress":
            logger.debug("api_reset_sent_scan: already running (job_id=%s)", existing["job_id"])
            return JSONResponse({"ok": False, "detail": "already running", "reset_sent_job": existing})

    # Set cancel immediately (fast DB write) so dashboard sees it
    logger.debug("api_reset_sent_scan: setting cancel_state=cancel_scans")
    with _srv.db.get_connection() as conn:
        _srv.db.set_cancel_state(conn, user_id, "cancel_scans")

    job_id = secrets.token_urlsafe(16)
    with _srv.db.get_connection() as conn:
        _srv.db.set_reset_sent_job(conn, user_id, job_id, "in_progress")

    # Spawn thread — it waits for scans to exit, then does the work
    _srv._spawn_scan_thread(_srv._run_reset_sent_scan, (user_id, job_id))
    return JSONResponse({"ok": True, "job_id": job_id})


@router.post("/api/actions/archive-unknown")
def api_archive_unknown(request: Request):
    """Start archiving all inbox messages with unknown-sender label."""
    session = _get_session(request)
    user_id = session["user_id"]
    logger.debug("api_archive_unknown: called for user %s", user_id)

    # Check for existing running job
    with _srv.db.get_connection() as conn:
        existing = _srv.db.get_archive_job(conn, user_id)
        if existing and existing["status"] == "in_progress":
            logger.debug("api_archive_unknown: already running (job_id=%s)", existing["job_id"])
            return JSONResponse({"ok": False, "detail": "already running", "archive_job": existing})

    # Set cancel immediately (fast DB write) so dashboard sees it
    logger.debug("api_archive_unknown: setting cancel_state=cancel_scans")
    with _srv.db.get_connection() as conn:
        _srv.db.set_cancel_state(conn, user_id, "cancel_scans")

    job_id = secrets.token_urlsafe(16)
    with _srv.db.get_connection() as conn:
        _srv.db.set_archive_job(conn, user_id, job_id, "starting")

    _srv._spawn_scan_thread(_srv._run_archive_unknown, (user_id, job_id))
    return JSONResponse({"ok": True, "job_id": job_id})


@router.post("/api/actions/cancel")
def api_cancel_action(request: Request):
    """Cancel whatever exclusive job is running for this user."""
    session = _get_session(request)
    user_id = session["user_id"]
    logger.debug("api_cancel_action: called for user %s", user_id)
    with _srv.db.get_connection() as conn:
        state = _srv.db.get_cancel_state(conn, user_id)
        if state != "cancel_scans":
            logger.debug("api_cancel_action: no running action (cancel_state=%s)", state)
            return JSONResponse({"ok": False, "detail": "no running action"})
        logger.debug("api_cancel_action: transitioning cancel_state from cancel_scans to cancel_job")
        _srv.db.set_cancel_state(conn, user_id, "cancel_job")
    return JSONResponse({"ok": True})
