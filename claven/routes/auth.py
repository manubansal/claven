"""Auth helpers and OAuth/logout routes."""

import logging
import os
import secrets
import time
from urllib.parse import urlencode, quote

import jwt as pyjwt

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

import claven.server as _srv

logger = logging.getLogger(__name__)

router = APIRouter()

# Sessions last 30 days; signing in again issues a new one.
SESSION_LIFETIME_SECONDS = 30 * 24 * 3600
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_internal_auth(request: Request) -> None:
    expected = f"Bearer {os.environ['INTERNAL_API_SECRET']}"
    if request.headers.get("Authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _verify_pubsub_token(request: Request) -> None:
    """Verify the OIDC bearer token Google attaches to Pub/Sub push deliveries.

    The token must be signed by Google for PUBSUB_AUDIENCE and identify
    PUBSUB_PUSH_SERVICE_ACCOUNT, the account Claven's push subscription signs
    with. Any Google Cloud project can push signed tokens at this URL, so
    "some service account" is not enough. Both settings are required: without
    them every push is refused.
    """
    audience = os.environ.get("PUBSUB_AUDIENCE")
    push_account = os.environ.get("PUBSUB_PUSH_SERVICE_ACCOUNT")
    if not audience or not push_account:
        logger.error("Webhook auth not configured (PUBSUB_AUDIENCE and PUBSUB_PUSH_SERVICE_ACCOUNT are required)")
        raise HTTPException(status_code=500, detail="Webhook auth not configured")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Pub/Sub auth token")

    token = auth_header[len("Bearer "):]

    try:
        id_info = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=audience,
        )
    except Exception as exc:
        logger.warning("Pub/Sub token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid Pub/Sub token")

    email = id_info.get("email", "")
    if email != push_account or id_info.get("email_verified") is not True:
        logger.warning("Pub/Sub token from unexpected identity: %s (verified=%s)", email, id_info.get("email_verified"))
        raise HTTPException(status_code=401, detail="Unexpected Pub/Sub service account")


def _issue_session(user_id: str, email: str, session_version: int) -> str:
    """Return a signed JWT encoding the user's identity.

    Accepted by all authenticated endpoints as either a 'session' cookie
    (browser) or an Authorization: Bearer token (CLI). It expires after
    SESSION_LIFETIME_SECONDS and carries the user's session version ('sv'),
    so bumping the version (logout) revokes it early.
    """
    now = int(time.time())
    return pyjwt.encode(
        {"user_id": user_id, "email": email, "iat": now, "exp": now + SESSION_LIFETIME_SECONDS,
         "sv": session_version},
        os.environ["SESSION_SECRET"],
        algorithm="HS256",
    )


def _get_session(request: Request) -> dict:
    """Extract and verify the session JWT from cookie or Bearer header.

    Checks the 'session' cookie first (browser path), then falls back to
    the Authorization: Bearer header (CLI path).  Raises 401 if missing,
    expired, revoked, or invalid so callers can treat the result as trusted.

    A state-changing request authenticated by the cookie is refused (403) when
    its Origin header names another site: the cookie is SameSite=Lax, and this
    backs it up. Requests without an Origin header (non-browser clients) pass;
    browsers always send one on cross-site POSTs.
    """
    token = request.cookies.get("session")
    if token:
        origin = request.headers.get("origin")
        if (request.method not in _SAFE_METHODS and origin is not None
                and origin not in _srv._allowed_origins()):
            logger.warning("Refused %s %s with session cookie from origin %s",
                           request.method, request.url.path, origin)
            raise HTTPException(status_code=403, detail="Cross-site request refused")
        logger.debug("_get_session: cookie session (method=%s, origin=%s)", request.method, origin)
    else:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer "):]
            logger.debug("_get_session: bearer session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        claims = pyjwt.decode(token, os.environ["SESSION_SECRET"], algorithms=["HS256"],
                              options={"require": ["exp", "iat"]})
    except pyjwt.PyJWTError as exc:
        logger.debug("_get_session: rejected token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid session")
    if not isinstance(claims.get("sv"), int):
        logger.debug("_get_session: token has no session version")
        raise HTTPException(status_code=401, detail="Invalid session")
    current = _current_session_version(claims.get("user_id"))
    if current != claims["sv"]:
        logger.debug("_get_session: session version %s, current %s", claims["sv"], current)
        raise HTTPException(status_code=401, detail="Session revoked")
    return claims


def _current_session_version(user_id: str) -> int | None:
    with _srv.db.get_connection() as conn:
        return _srv.db.get_session_version(conn, user_id)


def _make_flow():
    return _srv.Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["OAUTH_CLIENT_ID"],
                "client_secret": os.environ["OAUTH_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [os.environ["OAUTH_REDIRECT_URI"]],
            }
        },
        scopes=_srv.SCOPES,
    )


def _redirect_base(request: Request) -> str:
    """Return the frontend origin to redirect to after OAuth.

    Uses the oauth_return_to cookie (set by oauth/start) if present and
    in the allowed-origins list; otherwise falls back to FRONTEND_URL.
    """
    frontend_url = os.environ.get("FRONTEND_URL", "https://claven.app")
    return_to = request.cookies.get("oauth_return_to")
    if return_to and return_to in _srv._allowed_origins():
        return return_to
    return frontend_url


def _error_redirect(base: str, reason: str, detail: str | None = None) -> RedirectResponse:
    url = f"{base}/?error={reason}"
    if detail:
        url += f"&error_detail={quote(detail)}"
    response = RedirectResponse(url=url, status_code=302)
    response.delete_cookie("oauth_state")
    response.delete_cookie("oauth_return_to")
    return response


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/oauth/start")
def oauth_start(return_to: str | None = None, force_consent: bool = False):
    if return_to and return_to not in _srv._allowed_origins():
        raise HTTPException(status_code=400, detail="Invalid return_to")
    flow = _make_flow()
    flow.redirect_uri = os.environ["OAUTH_REDIRECT_URI"]
    state = secrets.token_urlsafe(32)
    # Use consent prompt only when forced (e.g. reconnecting after disconnect
    # where Google won't return a refresh token without explicit re-consent).
    prompt = "consent" if force_consent else "select_account"
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt=prompt,
        state=state,
    )
    response = RedirectResponse(url=auth_url)
    response.set_cookie("oauth_state", state, httponly=True, max_age=600, samesite="lax", secure=True)
    if return_to:
        response.set_cookie("oauth_return_to", return_to, httponly=True, max_age=600, samesite="lax", secure=True)
    return response


@router.get("/oauth/callback")
def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    base = _redirect_base(request)

    if error:
        logger.warning("OAuth error from Google: %s", error)
        return _error_redirect(base, "oauth_denied")
    if not code or not state:
        return _error_redirect(base, "invalid_request")

    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state:
        logger.warning("State mismatch: stored=%r param=%r", stored_state, state)
        return _error_redirect(base, "invalid_state")

    # State verified — delete the cookie immediately so it can't be replayed
    flow = _make_flow()
    flow.redirect_uri = os.environ["OAUTH_REDIRECT_URI"]
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.warning("Token exchange failed: %s", exc)
        return _error_redirect(base, "token_exchange_failed", str(exc))
    creds = flow.credentials

    # Google's granular permissions lets users uncheck individual scopes.
    # Verify the required gmail.modify scope was granted.
    granted = set(creds.scopes or [])
    if "https://www.googleapis.com/auth/gmail.modify" not in granted:
        logger.warning("Gmail scope not granted (got: %s)", granted)
        return _error_redirect(base, "gmail_scope_missing")

    try:
        id_info = google_id_token.verify_oauth2_token(
            creds.id_token,
            google_requests.Request(),
            os.environ["OAUTH_CLIENT_ID"],
        )
    except Exception as exc:
        logger.warning("ID token verification failed: %s", exc)
        return _error_redirect(base, "token_verification_failed", str(exc))
    email = id_info["email"]

    try:
        with _srv.db.get_connection() as conn:
            user_id = _srv.db.upsert_user(conn, email)
            existing_tokens = _srv.db.load_tokens(conn, user_id)
            if not existing_tokens or existing_tokens.get("revoked_at"):
                if not creds.refresh_token:
                    # User previously authorized the app but Google didn't
                    # return a refresh token (happens when reconnecting after
                    # disconnect, or when the stored one was revoked).
                    # Redirect back to force re-consent.
                    logger.debug("oauth_callback: no refresh token for %s (stored=%s), forcing consent",
                                 email, "revoked" if existing_tokens else "none")
                    api_base = os.environ["OAUTH_REDIRECT_URI"].rsplit("/oauth/callback", 1)[0]
                    params: dict = {"force_consent": "true"}
                    if base != os.environ.get("FRONTEND_URL", "https://claven.app"):
                        params["return_to"] = base
                    response = RedirectResponse(
                        url=f"{api_base}/oauth/start?{urlencode(params)}",
                        status_code=302,
                    )
                    response.delete_cookie("oauth_state")
                    response.delete_cookie("oauth_return_to")
                    return response
                # New user, or reconnecting after revoked access — store credentials only.
                _srv.auth.store_credentials(conn, user_id, creds, os.environ["TOKEN_ENCRYPTION_KEY"])
            elif creds.refresh_token:
                # Returning user with a fresh refresh token — update stored
                # credentials. This handles re-auth after token revocation.
                logger.info("Updating credentials for %s (new refresh token)", email)
                _srv.auth.store_credentials(conn, user_id, creds, os.environ["TOKEN_ENCRYPTION_KEY"])
    except Exception as exc:
        logger.exception("Signup failed for %s: %s", email, exc)
        return _error_redirect(base, "signup_failed", str(exc))

    logger.info("OAuth complete for %s (user_id=%s)", email, user_id,
                extra={"event": "oauth_complete", "user_id": user_id, "email": email})

    # No scan triggered here — /api/me handles retrigger logic on first
    # dashboard load, respecting cancel state and exclusive job ownership.

    with _srv.db.get_connection() as conn:
        session_version = _srv.db.get_session_version(conn, user_id)
    session_token = _issue_session(user_id, email, session_version)
    response = RedirectResponse(url=f"{base}/dashboard", status_code=302)
    response.delete_cookie("oauth_state")
    response.delete_cookie("oauth_return_to")
    # claven.app and api.claven.app are the same site, so Lax reaches the API
    # from the dashboard but not from other sites' requests.
    response.set_cookie("session", session_token, httponly=True, secure=True, samesite="lax",
                        max_age=SESSION_LIFETIME_SECONDS)
    return response


@router.post("/api/logout")
def api_logout(request: Request):
    session = _get_session(request)
    # Revokes every session the user has, not just this browser's.
    with _srv.db.get_connection() as conn:
        _srv.db.bump_session_version(conn, session["user_id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response
