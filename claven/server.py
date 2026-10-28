"""
Claven web server — thin HTTP entry point over claven/core/.

Endpoints:
  GET  /health                  — liveness probe for Cloud Run
  GET  /oauth/start             — begin OAuth flow, redirect to Google consent
  GET  /oauth/callback          — exchange OAuth code for tokens, store in DB
  POST /internal/poll           — Cloud Scheduler trigger: poll Gmail history for all users
  POST /internal/build-known-senders — build/update known senders list for all users
  POST /webhook/gmail           — Pub/Sub push handler: incoming Gmail notifications
"""

import logging
import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from google.oauth2 import id_token as google_id_token  # noqa: F401 — patched by tests
from google_auth_oauthlib.flow import Flow  # noqa: F401 — accessed via _srv.Flow by routes
import claven.core.auth as auth  # noqa: F401 — accessed via _srv.auth by routes/tasks
import claven.core.db as db
from claven.core.gmail import build_label_id_cache, get_profile  # noqa: F401
from claven.core.health import compute_scan_health  # noqa: F401
from claven.core.process import poll_new_messages  # noqa: F401
from claven.core.rules import load_config  # noqa: F401
from claven.core.scan import build_known_senders, relabel_scan, scan_inbox  # noqa: F401
from claven.core.watch import start_watch, stop_watch  # noqa: F401


class _CloudJsonFormatter(logging.Formatter):
    """Emit structured JSON for Cloud Logging. Maps Python levels to Cloud severity.

    Extra fields (user_id, event) are included when passed via logger.info("msg", extra={...}).
    """
    _SEVERITY = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record):
        import json as _json
        entry = {
            "severity": self._SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        for field in ("user_id", "event", "email"):
            if val := getattr(record, field, None):
                entry[field] = val
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(entry)


_LOG_FILE = os.environ.get("CLAVEN_LOG_FILE", "")
_ON_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))

if _LOG_FILE:
    # Local dev with file logging — human-readable + file
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(_LOG_FILE), logging.StreamHandler()],
        force=True,
    )
elif _ON_CLOUD_RUN:
    # Production on Cloud Run — structured JSON to stdout for Cloud Logging
    _handler = logging.StreamHandler()
    _handler.setFormatter(_CloudJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Worker generation ID — unique per process. Background scan threads check this
# per batch and exit if it changes (means --reload spawned a new worker).
_worker_id = os.getpid()

# Shutdown event — replaces a plain boolean flag. threading.Event lets
# background threads use event.wait(timeout) instead of time.sleep(),
# so they wake up instantly when shutdown is signalled instead of
# blocking for the full sleep duration.
_shutdown_event = threading.Event()

# Registry of active scan threads — joined during lifespan shutdown
# so the process exits cleanly instead of leaving orphans.
_active_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()
_resumed_jobs: set[str] = set()  # Guards against duplicate job resumption


def _shutdown_handler(signum, frame):
    _shutdown_event.set()
    # Don't log here — signal handlers can interrupt mid-flush, causing
    # "reentrant call inside BufferedWriter" on shutdown.


import signal
signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


def _spawn_scan_thread(target, args):
    """Start a daemon thread and track it for graceful shutdown."""
    t = threading.Thread(target=target, args=args, daemon=True)
    with _threads_lock:
        _active_threads.append(t)
    t.start()
    return t


@asynccontextmanager
async def lifespan(app):
    # Reset shutdown state — needed for test isolation since TestClient
    # enters/exits lifespan for every test.
    _shutdown_event.clear()
    with _threads_lock:
        _active_threads.clear()
    _resumed_jobs.clear()
    try:
        with db.get_connection() as conn:
            db.clear_cancel_job_flags(conn)
            logger.debug("lifespan: cleared cancel_job flags on startup")
    except Exception:
        logger.debug("lifespan: failed to clear cancel flags on startup (DB may be unavailable)")
    logger.info("Worker started (pid=%d)", _worker_id)
    yield
    # Shutdown: signal all threads to stop, then wait for them.
    logger.debug("lifespan: shutdown starting, setting _shutdown_event")
    _shutdown_event.set()
    with _threads_lock:
        threads = list(_active_threads)
    logger.debug("lifespan: joining %d active threads", len(threads))
    for t in threads:
        t.join(timeout=5)
    still_alive = sum(1 for t in threads if t.is_alive())
    if still_alive:
        logger.warning("Shutdown: %d scan thread(s) still alive after timeout", still_alive)
    with _threads_lock:
        _active_threads.clear()
    logger.info("Worker shutdown complete (pid=%d)", _worker_id)

app = FastAPI(title="Claven", lifespan=lifespan)

def _allowed_origins() -> list[str]:
    origins = [os.environ.get("FRONTEND_URL", "https://claven.app")]
    if extra := os.environ.get("CORS_EXTRA_ORIGINS", ""):
        origins += [o.strip() for o in extra.split(",") if o.strip()]
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


# ── Health endpoints (kept here — no dependencies on route modules) ──────────

@app.get("/health")
def health():
    """Liveness probe — always returns 200 if the process is running."""
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    """Readiness probe — checks DB connectivity. Returns 503 if DB is unreachable."""
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as exc:
        logger.error("Health check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "db": "unreachable", "detail": str(exc)},
        )


# ── Include route modules ───────────────────────────────────────────────────

from claven.routes.auth import router as auth_router
from claven.routes.api import router as api_router
from claven.routes.actions import router as actions_router
from claven.routes.internal import router as internal_router

app.include_router(auth_router)
app.include_router(api_router)
app.include_router(actions_router)
app.include_router(internal_router)

# ── Re-exports for backward compatibility (tests import from claven.server) ──

from claven.tasks import (  # noqa: F401, E402
    _needs_sent_scan,
    _is_current_worker,
    _cancel_scans_and_wait,
    _should_continue_scan,
    _should_continue_job,
    _run_inbox_scan,
    _run_relabel_scan,
    _run_sent_scan,
    _run_archive_unknown,
    _run_reset_sent_scan,
    _label_id_cache_for_config,
    _sunset_for_live_labeling,
)
