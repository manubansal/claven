"""Dashboard API routes (/api/me, /api/config, /api/connect, /api/disconnect, /api/settings/scan-scope, /api/events)."""

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import claven.server as _srv
from claven.core.gmail import migrate_legacy_labels
from claven.core.health import HEALTH_CODES
from claven.core.labels import KNOWN, LEGACY_NAMES
from claven.core.sunset import COUNTDOWN_LABELS
from claven.routes.auth import _get_session

logger = logging.getLogger(__name__)

router = APIRouter()


def _log_health(health: dict | None, user_id: str) -> dict | None:
    """Log non-ok health codes and return the health dict unchanged."""
    if health and health["severity"] in ("warning", "error"):
        logger.warning("Scan health %s: %s (user=%s)", health["code"], health["label"], user_id,
                        extra={"event": "scan_health", "user_id": user_id})
    return health


@router.get("/api/me")
def api_me(request: Request):
    session = _get_session(request)
    with _srv.db.get_connection() as conn:
        user = _srv.db.get_user_by_id(conn, session["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        history_id = _srv.db.get_history_id(conn, session["user_id"])
        known_senders = _srv.db.count_known_senders(conn, session["user_id"])
        pending_relabel_count = len(_srv.db.get_pending_relabel_senders(conn, session["user_id"]))
        sent_scan_progress = _srv.db.get_sent_scan_progress(conn, session["user_id"])
        # processed_count no longer used — progress derived from live Gmail label counts
        last_labeled_at = _srv.db.get_last_labeled_at(conn, session["user_id"])
        last_fetched_at = _srv.db.get_last_fetched_at(conn, session["user_id"])
        scan_scope = _srv.db.get_scan_scope(conn, session["user_id"])
        inbox_mode = _srv.db.get_inbox_mode(conn, session["user_id"])
        auto_archive_unknown = inbox_mode["mode"] == "gatekeeper"

        unread_count = None
        read_count = None
        inbox_count = None
        all_mail_count = None
        sent_total_live = None
        sent_scanned_count = 0
        newest_mail_at = None
        newest_labeled_at = None
        allmail_labeled_known_count = None
        allmail_labeled_unknown_count = None
        allmail_labeled_total_count = None
        inbox_labeled_known_shallow_count = None
        inbox_labeled_known_has_more = None
        inbox_labeled_unknown_shallow_count = None
        inbox_labeled_unknown_has_more = None
        inbox_unlabeled_first_page_count = None
        inbox_unlabeled_deep_count = None
        allmail_unlabeled_first_page_count = None
        scan_unlabeled_first_page_count = None
        gmail_error = None
        try:
            service = _srv.auth.get_service(conn, session["user_id"], os.environ["TOKEN_ENCRYPTION_KEY"])

            # ── Batch 1: all independent Gmail calls ─────────────────
            b1 = {}
            def _b1_cb(rid, resp, exc):
                if not exc:
                    b1[rid] = resp
            batch1 = service.new_batch_http_request(callback=_b1_cb)
            batch1.add(service.users().labels().get(userId="me", id="INBOX"), request_id="inbox")
            batch1.add(service.users().labels().get(userId="me", id="SENT"), request_id="sent")
            batch1.add(service.users().labels().list(userId="me"), request_id="labels")
            # read_count derived from inbox_count - unread_count (both exact from labels.get)
            batch1.add(service.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=1), request_id="newest_msg")
            label_configs = _srv.load_config().get("labels", [])
            batch1.add(service.users().getProfile(userId="me"), request_id="profile")
            batch1.execute()

            inbox_data = b1.get("inbox", {})
            unread_count = inbox_data.get("messagesUnread")
            inbox_count = inbox_data.get("messagesTotal")
            read_count = (inbox_count - unread_count) if inbox_count is not None and unread_count is not None else None
            all_mail_count = b1.get("profile", {}).get("messagesTotal")
            sent_total_live = b1.get("sent", {}).get("messagesTotal")

            all_gmail_labels = b1.get("labels", {}).get("labels", [])
            if LEGACY_NAMES.keys() & {l["name"] for l in all_gmail_labels}:
                logger.debug("api_me: renaming labels from an older Claven version")
                all_gmail_labels = migrate_legacy_labels(service, all_gmail_labels)
            else:
                logger.debug("api_me: labels already current")
            label_id_by_name = {l["name"]: l["id"] for l in all_gmail_labels}
            # In Sunset, waiting unknown-sender mail carries a countdown label instead of the
            # unknown label. Up to 72 of them, so they are counted with labels.get only.
            countdown_names = [n for n in COUNTDOWN_LABELS if n in label_id_by_name]
            countdown_ids = [label_id_by_name[n] for n in countdown_names]

            newest_msgs = b1.get("newest_msg", {}).get("messages", [])

            # ── Batch 2: calls that depend on batch 1 results ────────
            from claven.core.scan import SENT_SCANNED_LABEL, _unlabeled_query

            b2 = {}
            def _b2_cb(rid, resp, exc):
                if not exc:
                    b2[rid] = resp
            batch2 = service.new_batch_http_request(callback=_b2_cb)

            sent_scanned_label_id = next((l["id"] for l in all_gmail_labels if l["name"] == SENT_SCANNED_LABEL), None)
            if sent_scanned_label_id:
                batch2.add(service.users().labels().get(userId="me", id=sent_scanned_label_id), request_id="sent_scanned")

            for lc in label_configs:
                if lid := label_id_by_name.get(lc["id"]):
                    batch2.add(service.users().labels().get(userId="me", id=lid), request_id=f"known_{lc['id']}")
                if (unknown := lc.get("unknown_label")) and (uid := label_id_by_name.get(unknown)):
                    batch2.add(service.users().labels().get(userId="me", id=uid), request_id=f"unknown_{unknown}")
            for cid in countdown_ids:
                batch2.add(service.users().labels().get(userId="me", id=cid), request_id=f"countdown_{cid}")

            if newest_msgs:
                batch2.add(service.users().messages().get(userId="me", id=newest_msgs[0]["id"], format="minimal"), request_id="newest_detail")

            # Newest labeled message — one call per label (labelIds is AND logic),
            # take the newer result. Uses labelIds (message-level) not q (thread-level)
            # so we get the actual labeled message, not a newer reply in the same thread.
            for lc in label_configs:
                if lid := label_id_by_name.get(lc["id"]):
                    batch2.add(service.users().messages().list(userId="me", labelIds=[lid], maxResults=1), request_id=f"newest_known_{lc['id']}")
                if (unknown := lc.get("unknown_label")) and (uid := label_id_by_name.get(unknown)):
                    batch2.add(service.users().messages().list(userId="me", labelIds=[uid], maxResults=1), request_id=f"newest_unknown_{unknown}")

            # Always query both inbox and allmail unlabeled counts.
            # Inbox unlabeled for display; allmail unlabeled for display + retrigger (when scope=allmail).
            inbox_unlabeled_q = _unlabeled_query(label_configs, scope="inbox", countdown_labels=countdown_names)
            allmail_unlabeled_q = _unlabeled_query(label_configs, scope="allmail", countdown_labels=countdown_names)
            batch2.add(service.users().messages().list(userId="me", q=inbox_unlabeled_q, maxResults=500), request_id="unlabeled")
            batch2.add(service.users().messages().list(userId="me", q=allmail_unlabeled_q, maxResults=500), request_id="allmail_unlabeled")

            # Shallow count of inbox known/unknown-sender messages
            for lc in label_configs:
                if lid := label_id_by_name.get(lc["id"]):
                    batch2.add(service.users().messages().list(userId="me", labelIds=["INBOX", lid], maxResults=500), request_id="inbox_known_shallow")
                if (unknown := lc.get("unknown_label")) and (uid := label_id_by_name.get(unknown)):
                    batch2.add(service.users().messages().list(userId="me", labelIds=["INBOX", uid], maxResults=500), request_id="inbox_unknown_shallow")

            batch2.execute()

            # ── Extract batch 2 results ──────────────────────────────
            sent_scanned_count = b2.get("sent_scanned", {}).get("messagesTotal", 0)

            allmail_labeled_known_count = 0
            allmail_labeled_unknown_count = 0
            for lc in label_configs:
                allmail_labeled_known_count += b2.get(f"known_{lc['id']}", {}).get("messagesTotal", 0)
                if unknown := lc.get("unknown_label"):
                    allmail_labeled_unknown_count += b2.get(f"unknown_{unknown}", {}).get("messagesTotal", 0)
            countdown_totals = {cid: b2.get(f"countdown_{cid}", {}).get("messagesTotal", 0) for cid in countdown_ids}
            allmail_labeled_unknown_count += sum(countdown_totals.values())
            allmail_labeled_total_count = allmail_labeled_known_count + allmail_labeled_unknown_count

            inbox_known_data = b2.get("inbox_known_shallow", {})
            inbox_known_msgs = inbox_known_data.get("messages", [])
            inbox_labeled_known_shallow_count = len(inbox_known_msgs)
            inbox_labeled_known_has_more = "nextPageToken" in inbox_known_data

            inbox_unknown_data = b2.get("inbox_unknown_shallow", {})
            inbox_unknown_msgs = inbox_unknown_data.get("messages", [])
            inbox_labeled_unknown_shallow_count = len(inbox_unknown_msgs)
            inbox_labeled_unknown_has_more = "nextPageToken" in inbox_unknown_data
            # Countdown labels are only on mail waiting in the inbox (the sweep swaps them back to
            # the unknown label within 15 minutes of the mail leaving), so their totals are inbox counts.
            inbox_labeled_unknown_shallow_count += sum(countdown_totals.values())

            newest_detail = b2.get("newest_detail")
            if newest_detail:
                newest_mail_ms = newest_detail.get("internalDate")
                if newest_mail_ms:
                    newest_mail_at = datetime.fromtimestamp(int(newest_mail_ms) / 1000, tz=timezone.utc)

            # Find newest labeled message across all label queries.
            # Each query returns newest-first, so we fetch the top candidate
            # from each label and pick the one with the highest internalDate.
            labeled_candidate_ids = []
            for lc in label_configs:
                for key in [f"newest_known_{lc['id']}", f"newest_unknown_{lc.get('unknown_label', '')}"]:
                    msgs = b2.get(key, {}).get("messages", [])
                    if msgs:
                        labeled_candidate_ids.append(msgs[0]["id"])
            # The newest waiting mail has the most time left: the longest countdown in use.
            if longest := next((cid for cid in countdown_ids if countdown_totals[cid]), None):
                msgs = service.users().messages().list(userId="me", labelIds=[longest], maxResults=1).execute().get("messages", [])
                labeled_candidate_ids.extend(m["id"] for m in msgs)
            else:
                logger.debug("api_me: no mail counting down")

            if labeled_candidate_ids:
                b3 = {}
                def _b3_cb(rid, resp, exc):
                    if not exc:
                        b3[rid] = resp
                batch3 = service.new_batch_http_request(callback=_b3_cb)
                for cid in labeled_candidate_ids:
                    batch3.add(service.users().messages().get(userId="me", id=cid, format="minimal"), request_id=cid)
                batch3.execute()

                best_ms = 0
                for cid in labeled_candidate_ids:
                    detail = b3.get(cid, {})
                    ms = int(detail.get("internalDate", 0) or 0)
                    if ms > best_ms:
                        best_ms = ms
                if best_ms:
                    newest_labeled_at = datetime.fromtimestamp(best_ms / 1000, tz=timezone.utc)

            unlabeled_data = b2.get("unlabeled", {})
            first_page_messages = unlabeled_data.get("messages", [])
            inbox_unlabeled_first_page_count = len(first_page_messages)

            # Paginate remaining inbox unlabeled for deep count
            total_unlabeled = inbox_unlabeled_first_page_count
            page_token = unlabeled_data.get("nextPageToken")
            while page_token:
                page = service.users().messages().list(
                    userId="me", q=inbox_unlabeled_q, maxResults=500,
                    pageToken=page_token,
                ).execute()
                total_unlabeled += len(page.get("messages", []))
                page_token = page.get("nextPageToken")
            inbox_unlabeled_deep_count = total_unlabeled

            # Allmail unlabeled — direct query (not derived from subtraction)
            allmail_unlabeled_data = b2.get("allmail_unlabeled", {})
            allmail_unlabeled_first_page_count = len(allmail_unlabeled_data.get("messages", []))

            # Scan-scope unlabeled for retrigger (same as inbox when scope=inbox)
            if scan_scope == "allmail":
                scan_unlabeled_first_page_count = allmail_unlabeled_first_page_count
            else:
                scan_unlabeled_first_page_count = inbox_unlabeled_first_page_count

            _srv.db.touch_last_fetched(conn, session["user_id"])
        except Exception as exc:
            from claven.tasks import _handle_error
            error_label = _handle_error(exc, session["user_id"], "Gmail API", conn)
            health_entry = HEALTH_CODES.get(error_label)
            gmail_error = health_entry if health_entry else {"code": error_label, "label": error_label, "severity": "error"}

        inbox_scan_status = _srv.db.get_inbox_scan_status(conn, session["user_id"])
        archive_job = _srv.db.get_archive_job(conn, session["user_id"])
        reset_sent_job = _srv.db.get_reset_sent_job(conn, session["user_id"])
        cancel_state = _srv.db.get_cancel_state(conn, session["user_id"])
        recent_events = _srv.db.get_recent_events(conn, session["user_id"])

    # Priority 1: if cancel_scans is set, an exclusive job owns the session.
    # Resume it if not already running (crash recovery). Guard with a set
    # to prevent /api/me from spawning duplicates on every call.
    logger.debug("/api/me: priority logic — cancel_state=%s, inbox_scan_status=%s, unlabeled_first_page=%s",
                 cancel_state, inbox_scan_status, inbox_unlabeled_first_page_count)
    if cancel_state == "cancel_scans":
        _resume_key = f"{session['user_id']}"
        if _resume_key not in _srv._resumed_jobs:
            _srv._resumed_jobs.add(_resume_key)
            if archive_job and archive_job["status"] in ("starting", "in_progress"):
                logger.debug("/api/me: resuming archive job %s (status=%s)", archive_job["job_id"], archive_job["status"])
                _srv._spawn_scan_thread(_srv._run_archive_unknown, (session["user_id"], archive_job["job_id"]))
            elif reset_sent_job and reset_sent_job["status"] in ("starting", "in_progress"):
                logger.debug("/api/me: resuming reset_sent job %s (status=%s)", reset_sent_job["job_id"], reset_sent_job["status"])
                _srv._spawn_scan_thread(_srv._run_reset_sent_scan, (session["user_id"], reset_sent_job["job_id"]))
            else:
                logger.debug("/api/me: cancel_scans set but no resumable job found (archive=%s, reset=%s)",
                             archive_job, reset_sent_job)
        else:
            logger.debug("/api/me: cancel_scans set but resume_key already in _resumed_jobs")

    if cancel_state is None:
        # No exclusive job active — clear stale job states
        if archive_job and archive_job["status"] in ("starting", "in_progress"):
            logger.debug("/api/me: clearing stale archive job %s (status=%s)", archive_job["job_id"], archive_job["status"])
            with _srv.db.get_connection() as conn:
                _srv.db.set_archive_job(conn, session["user_id"], archive_job["job_id"], "error")
                _srv.db.log_event(conn, session["user_id"], "error", "Archive job stale — cleared")
            archive_job["status"] = "error"
        if reset_sent_job and reset_sent_job["status"] in ("starting", "in_progress"):
            logger.debug("/api/me: clearing stale reset_sent job %s (status=%s)", reset_sent_job["job_id"], reset_sent_job["status"])
            with _srv.db.get_connection() as conn:
                _srv.db.set_reset_sent_job(conn, session["user_id"], reset_sent_job["job_id"], "error")
                _srv.db.log_event(conn, session["user_id"], "error", "Reset sent scan job stale — cleared")
            reset_sent_job["status"] = "error"

        # Auto-reset stalled scans
        scan_health = _srv.compute_scan_health(inbox_scan_status, last_fetched_at)
        if scan_health and scan_health["label"] == "warning.scan.stalled":
            logger.debug("/api/me: resetting stalled scan (was %s, last_fetched=%s)", inbox_scan_status, last_fetched_at)
            inbox_scan_status = "error.scan.stalled"
            with _srv.db.get_connection() as conn:
                _srv.db.set_inbox_scan_status(conn, session["user_id"], inbox_scan_status)
                _srv.db.log_event(conn, session["user_id"], "error", "Label scan stalled — reset to error")
            logger.warning("Reset stalled scan for %s", session["user_id"],
                           extra={"event": "scan_stalled_reset", "user_id": session["user_id"]})

        # Auto-clear errors when no work remains
        if (inbox_scan_status and inbox_scan_status.startswith("error")
                and (inbox_unlabeled_first_page_count is not None and inbox_unlabeled_first_page_count == 0)):
            logger.debug("/api/me: auto-clearing error (status=%s, unlabeled=0)", inbox_scan_status)
            with _srv.db.get_connection() as conn:
                _srv.db.set_inbox_scan_status(conn, session["user_id"], "complete")
                _srv.db.log_event(conn, session["user_id"], "scan", f"Label scan error auto-cleared ({inbox_scan_status}) — no unlabeled remaining")
            inbox_scan_status = "complete"

        # Auto-retrigger scan chain — but not if sent scan is actively running
        # (non-stale in_progress). It chains into inbox scan on completion.
        # Uses scan_unlabeled (scope-aware) to decide if there's work to do.
        sent_running = (sent_scan_progress["status"] == "in_progress"
                        and not _srv._needs_sent_scan(sent_scan_progress))
        if (scan_unlabeled_first_page_count is not None
                and scan_unlabeled_first_page_count > 0
                and inbox_scan_status != "in_progress"
                and not sent_running
                and history_id is not None):
            logger.debug("/api/me: auto-retriggering scan chain (scan_unlabeled=%d, inbox_status=%s, sent_status=%s)",
                         scan_unlabeled_first_page_count, inbox_scan_status, sent_scan_progress["status"])
            _srv._spawn_scan_thread(_srv._run_sent_scan, (session["user_id"],))
        elif (pending_relabel_count > 0
                and inbox_scan_status != "in_progress"
                and not sent_running
                and history_id is not None):
            logger.debug("/api/me: auto-triggering relabel scan (pending_relabel=%d)", pending_relabel_count)
            _srv._spawn_scan_thread(_srv._run_relabel_scan, (session["user_id"],))
        else:
            logger.debug("/api/me: no retrigger (scan_unlabeled=%s, inbox_status=%s, sent_running=%s, pending_relabel=%d, history=%s)",
                         scan_unlabeled_first_page_count, inbox_scan_status, sent_running, pending_relabel_count, history_id)

    return {
        "email": user["email"],
        "connected": history_id is not None,
        "history_id": history_id,
        "known_senders": known_senders,
        "pending_relabel_count": pending_relabel_count,
        "sent_scanned_count": sent_scanned_count,
        "sent_total_count": sent_total_live,
        "sent_scan_status": sent_scan_progress["status"],
        "sent_scan_health": (
            HEALTH_CODES.get(sent_scan_progress["status"])
            or (HEALTH_CODES.get("warning.scan.cancelled") if sent_scan_progress["status"] == "cancelled" else None)
            or (HEALTH_CODES.get("info.waiting") if sent_scan_progress["status"] is None else None)
        ),
        "inbox_scan_status": inbox_scan_status,
        "scan_health": _log_health(_srv.compute_scan_health(inbox_scan_status, last_fetched_at), session["user_id"]),
        "last_fetched_at": last_fetched_at.isoformat() if last_fetched_at else None,
        "last_labeled_at": last_labeled_at.isoformat() if last_labeled_at else None,
        "newest_mail_at": newest_mail_at.isoformat() if newest_mail_at else None,
        "newest_labeled_at": newest_labeled_at.isoformat() if newest_labeled_at else None,
        "allmail_labeled_known_count": allmail_labeled_known_count,
        "allmail_labeled_unknown_count": allmail_labeled_unknown_count,
        "allmail_labeled_total_count": allmail_labeled_total_count,
        "inbox_unlabeled_first_page_count": inbox_unlabeled_first_page_count,
        "inbox_unlabeled_deep_count": inbox_unlabeled_deep_count,
        "inbox_labeled_known_shallow_count": inbox_labeled_known_shallow_count,
        "inbox_labeled_known_has_more": inbox_labeled_known_has_more,
        "inbox_labeled_unknown_shallow_count": inbox_labeled_unknown_shallow_count,
        "inbox_labeled_unknown_has_more": inbox_labeled_unknown_has_more,
        "archive_job": archive_job,
        "reset_sent_job": reset_sent_job,
        "recent_events": recent_events,
        "scan_scope": scan_scope,
        "auto_archive_unknown": auto_archive_unknown,
        "inbox_mode": inbox_mode["mode"],
        "sunset_hours": inbox_mode["sunset_hours"],
        "cancel_state": cancel_state,
        "unread_count": unread_count,
        "read_count": read_count,
        "inbox_count": inbox_count,
        "all_mail_count": all_mail_count,
        "allmail_unlabeled_first_page_count": allmail_unlabeled_first_page_count,
        "gmail_error": gmail_error,
    }


@router.get("/api/config")
def api_config():
    config = _srv.load_config()
    return {"labels": config.get("labels", [])}


def _listen_database_url() -> str:
    """Connection string for the SSE LISTEN connection.

    DATABASE_URL points at Neon's PgBouncer pooler (transaction mode), which
    accepts LISTEN but never delivers notifications. DATABASE_URL_DIRECT is
    the non-pooled host; fall back to DATABASE_URL for local dev and CI,
    where there is no pooler.
    """
    direct = os.environ.get("DATABASE_URL_DIRECT")
    if direct:
        logger.debug("_listen_database_url: using DATABASE_URL_DIRECT")
        return direct
    logger.debug("_listen_database_url: DATABASE_URL_DIRECT unset, using DATABASE_URL")
    return os.environ["DATABASE_URL"]


@router.get("/api/events")
async def api_events(request: Request):
    """Server-Sent Events endpoint for live scan progress.

    Listens on Postgres NOTIFY 'scan_progress' and forwards events
    matching the authenticated user's ID. Workers send NOTIFY after
    each batch commit, so the dashboard updates in real-time without
    polling.

    The connection stays open until the client disconnects. Sends a
    heartbeat comment every 30s to keep the connection alive through
    load balancers (Cloud Run, Cloudflare).

    Cost: one Postgres LISTEN connection per SSE client. No CPU while
    idle — select() blocks in a thread without burning cycles.
    """
    import asyncio
    import select
    import psycopg2
    import psycopg2.extensions as pg_ext

    session = _get_session(request)
    user_id = session["user_id"]

    def _open_listen_conn():
        conn = psycopg2.connect(_listen_database_url())
        conn.set_isolation_level(pg_ext.ISOLATION_LEVEL_AUTOCOMMIT)
        with conn.cursor() as cur:
            cur.execute("LISTEN scan_progress")
        return conn

    listen_conn = None
    try:
        listen_conn = _open_listen_conn()
    except Exception as exc:
        if listen_conn:
            listen_conn.close()
        logger.warning("SSE: failed to open LISTEN connection: %s", exc)
        raise HTTPException(status_code=503, detail="Event stream unavailable")

    _HEARTBEAT_INTERVAL = 30  # seconds
    _SELECT_TIMEOUT = 5       # seconds — check disconnect + heartbeat periodically

    async def event_stream():
        nonlocal listen_conn
        last_heartbeat = asyncio.get_event_loop().time()
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                # Poll Postgres for notifications (non-blocking via asyncio)
                try:
                    ready = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: select.select([listen_conn], [], [], _SELECT_TIMEOUT)
                    )
                    if ready[0]:
                        listen_conn.poll()
                        while listen_conn.notifies:
                            notify = listen_conn.notifies.pop(0)
                            try:
                                payload = json.loads(notify.payload)
                            except Exception:
                                continue
                            if payload.get("user_id") != user_id:
                                continue
                            yield f"data: {notify.payload}\n\n"
                except psycopg2.OperationalError:
                    # Neon suspends idle connections — reconnect
                    try:
                        listen_conn.close()
                    except Exception:
                        logger.debug("SSE: failed to close dead LISTEN connection")
                    try:
                        listen_conn = _open_listen_conn()
                    except Exception as exc:
                        logger.warning("SSE: failed to reconnect LISTEN connection: %s", exc)
                        break

                # Heartbeat — keeps connection alive through load balancers
                now = asyncio.get_event_loop().time()
                if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                    yield ": keepalive\n\n"
                    last_heartbeat = now
        except (GeneratorExit, asyncio.CancelledError):
            pass
        finally:
            try:
                listen_conn.close()
            except Exception:
                logger.debug("SSE: failed to close LISTEN connection on cleanup")

    from starlette.responses import StreamingResponse
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/connect")
def api_connect(request: Request):
    """Start the Gmail push watch for the authenticated user.

    This is the explicit user-initiated step that begins inbox filtering.
    Credentials must already be stored (via the OAuth sign-in flow).
    Also kicks off a background scan of Sent mail to build the known senders list.
    """
    session = _get_session(request)
    with _srv.db.get_connection() as conn:
        try:
            service = _srv.auth.get_service(conn, session["user_id"], os.environ["TOKEN_ENCRYPTION_KEY"])
            watch_response = _srv.start_watch(service, os.environ["PUBSUB_TOPIC"])
            history_id = int(watch_response["historyId"])
            _srv.db.set_history_id(conn, session["user_id"], history_id)
        except Exception as exc:
            logger.exception("Connect failed for %s: %s", session["email"], exc)
            raise HTTPException(status_code=500, detail="Failed to start Gmail watch")
    # Kick off sent scan only — it chains into inbox scan on completion.
    # Do NOT spawn a separate inbox scan thread: it races with the sent
    # scan and can label messages with an incomplete known_senders set.
    _srv._spawn_scan_thread(_srv._run_sent_scan, (session["user_id"],))
    return JSONResponse({"ok": True, "history_id": history_id})


@router.post("/api/disconnect")
def api_disconnect(request: Request):
    """Stop the Gmail push watch and clear scan state.

    Keeps OAuth credentials intact so the user can reconnect with a single
    click (no OAuth round-trip required).
    """
    session = _get_session(request)
    with _srv.db.get_connection() as conn:
        try:
            service = _srv.auth.get_service(conn, session["user_id"], os.environ["TOKEN_ENCRYPTION_KEY"])
            _srv.stop_watch(service)
        except Exception as exc:
            logger.warning("stop_watch failed during disconnect for %s: %s", session["email"], exc)
        _srv.db.clear_watch_state(conn, session["user_id"])
    return JSONResponse({"ok": True})


@router.post("/api/settings/scan-scope")
async def api_set_scan_scope(request: Request):
    """Set the scan scope to 'inbox' or 'allmail'."""
    session = _get_session(request)
    body = await request.json()
    scope = body.get("scope")
    if scope not in ("inbox", "allmail"):
        raise HTTPException(status_code=400, detail="scope must be 'inbox' or 'allmail'")
    with _srv.db.get_connection() as conn:
        old_scope = _srv.db.get_scan_scope(conn, session["user_id"])
        _srv.db.set_scan_scope(conn, session["user_id"], scope)
        if old_scope != scope:
            # Reset inbox scan status so retrigger can fire for the new scope
            _srv.db.set_inbox_scan_status(conn, session["user_id"], None)
            _srv.db.log_event(conn, session["user_id"], "setting", f"Scan scope changed to {scope}")
            logger.debug("api_set_scan_scope: %s → %s, reset inbox_scan_status", old_scope, scope)
    return JSONResponse({"ok": True, "scan_scope": scope})


@router.post("/api/settings/auto-archive")
async def api_set_auto_archive(request: Request):
    """Enable or disable auto-archive for unknown-sender messages."""
    session = _get_session(request)
    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be true or false")
    with _srv.db.get_connection() as conn:
        _srv.db.set_auto_archive_unknown(conn, session["user_id"], enabled)
        _srv.db.log_event(conn, session["user_id"], "setting", f"Auto-archive unknown-sender {'enabled' if enabled else 'disabled'}")
    return JSONResponse({"ok": True, "auto_archive_unknown": enabled})


_INBOX_MODE_NAMES = {"label_only": "Label only", "gatekeeper": "Gatekeeper", "sunset": "Sunset"}


@router.post("/api/settings/inbox-mode")
async def api_set_inbox_mode(request: Request):
    """Pick what happens to unknown-sender mail: label_only, gatekeeper, or sunset.

    sunset_hours (optional) sets the Sunset timer; omitted keeps the current one.
    """
    from claven.core.sunset import INBOX_MODES, SUNSET_HOURS_CHOICES

    session = _get_session(request)
    body = await request.json()
    mode = body.get("mode")
    sunset_hours = body.get("sunset_hours")
    if mode not in INBOX_MODES:
        logger.debug("api_set_inbox_mode: invalid mode %r", mode)
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(INBOX_MODES)}")
    if sunset_hours is not None and sunset_hours not in SUNSET_HOURS_CHOICES:
        logger.debug("api_set_inbox_mode: invalid sunset_hours %r", sunset_hours)
        raise HTTPException(status_code=400, detail=f"sunset_hours must be one of {SUNSET_HOURS_CHOICES}")
    with _srv.db.get_connection() as conn:
        _srv.db.set_inbox_mode(conn, session["user_id"], mode, sunset_hours=sunset_hours)
        detail = f" ({sunset_hours}h)" if mode == "sunset" and sunset_hours else ""
        _srv.db.log_event(conn, session["user_id"], "setting", f"Inbox mode set to {_INBOX_MODE_NAMES[mode]}{detail}")
    logger.debug("api_set_inbox_mode: %s → %s hours=%s", session["user_id"], mode, sunset_hours)
    return JSONResponse({"ok": True, "inbox_mode": mode, "sunset_hours": sunset_hours})


@router.get("/api/top-senders")
def api_top_senders(request: Request):
    """Return top known senders by unread inbox message count (descending).

    Fetches up to 500 unread known-sender inbox messages, extracts From headers
    via batch API, counts by sender, returns top 10.
    """
    session = _get_session(request)
    with _srv.db.get_connection() as conn:
        service = _srv.auth.get_service(conn, session["user_id"], os.environ["TOKEN_ENCRYPTION_KEY"])

    from claven.core.gmail import gmail_retry, batch_get_message_metadata
    from claven.core.scan import _parse_addresses

    # Query unread known-sender inbox messages
    label_configs = _srv.load_config().get("labels", [])
    known_label = label_configs[0]["id"] if label_configs else KNOWN

    query = f"in:inbox is:unread label:{known_label}"
    result = gmail_retry(lambda: service.users().messages().list(
        userId="me", q=query, maxResults=500,
    ).execute())
    messages = result.get("messages", [])

    if not messages:
        return {"top_senders": [], "total_unread_known": 0}

    # Batch-fetch From headers (50 at a time)
    from collections import Counter
    sender_counts: Counter = Counter()
    batch_size = 50

    for i in range(0, len(messages), batch_size):
        batch_ids = [m["id"] for m in messages[i:i + batch_size]]
        metadata = batch_get_message_metadata(service, batch_ids, ["From"])
        for msg_id, (headers, _, _) in metadata.items():
            from_value = headers.get("from", "")
            if from_value:
                # Extract email address from "Name <email>" format
                addrs = _parse_addresses(from_value)
                if addrs:
                    sender_counts[addrs[0].lower()] += 1

    # Aggregate by domain
    domain_counts: Counter = Counter()
    for email, count in sender_counts.items():
        domain = email.split("@", 1)[1] if "@" in email else email
        domain_counts[domain] += count

    top_10_senders = sender_counts.most_common(10)
    top_10_domains = domain_counts.most_common(10)
    return {
        "top_senders": [{"email": email, "count": count} for email, count in top_10_senders],
        "top_domains": [{"domain": domain, "count": count} for domain, count in top_10_domains],
        "total_unread_known": len(messages),
    }
