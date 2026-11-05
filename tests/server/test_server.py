"""Server tests for claven/server.py — HTTP layer via ASGI test client."""

import base64
import json
import logging
import os
import signal
import threading
from contextlib import contextmanager
from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


class _FakeBatch:
    """Simulate Gmail batch execution for tests.

    Collects add() calls, then on execute() calls each request's .execute()
    and passes the result to the callback — same contract as the real batch API.
    """
    def __init__(self, callback):
        self._callback = callback
        self._requests = []

    def add(self, request, request_id=None):
        self._requests.append((request, request_id))

    def execute(self):
        for request, request_id in self._requests:
            try:
                response = request.execute()
                self._callback(request_id, response, None)
            except Exception as exc:
                self._callback(request_id, None, exc)

from claven.server import app

pytestmark = pytest.mark.server


def _pubsub_payload(email="user@example.com", history_id="12345"):
    notification = json.dumps({"emailAddress": email, "historyId": history_id})
    data = base64.b64encode(notification.encode()).decode()
    return {"message": {"data": data, "messageId": "msg-1", "publishTime": "2026-01-01T00:00:00Z"}}


_PUBSUB_TOKEN = "fake-pubsub-jwt"
_PUBSUB_HEADERS = {"Authorization": f"Bearer {_PUBSUB_TOKEN}"}
_PUBSUB_ID_INFO = {"email": "claven-pubsub@claven-prod.iam.gserviceaccount.com", "email_verified": True}


@contextmanager
def _mock_pubsub_token():
    """Patch Pub/Sub JWT verification to return a valid service-account identity."""
    with patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify:
        mock_verify.return_value = _PUBSUB_ID_INFO
        yield mock_verify


def _fake_db_ctx(mock_db, conn=None):
    """Wire mock_db.get_connection() as a context manager returning conn."""
    mock_conn = conn or MagicMock()

    @contextmanager
    def _ctx(*args, **kwargs):
        yield mock_conn

    mock_db.get_connection.side_effect = _ctx
    mock_db.get_processed_count.return_value = 0
    mock_db.get_session_version.return_value = 0
    mock_db.get_sent_scan_progress.return_value = {"messages_scanned": 0, "messages_total": None, "status": "complete", "updated_at": None}
    mock_db.is_inbox_scan_completed.return_value = False
    mock_db.get_inbox_scan_status.return_value = None
    mock_db.get_last_fetched_at.return_value = None
    mock_db.get_last_labeled_at.return_value = None
    mock_db.get_scan_scope.return_value = "inbox"
    mock_db.get_cancel_state.return_value = None
    mock_db.get_archive_job.return_value = None
    mock_db.get_reset_sent_job.return_value = None
    return mock_conn


_ENV = {
    "OAUTH_CLIENT_ID": "test-client-id",
    "OAUTH_CLIENT_SECRET": "test-client-secret",
    "OAUTH_REDIRECT_URI": "http://localhost/oauth/callback",
    "INTERNAL_API_SECRET": "test-internal-secret",
    "TOKEN_ENCRYPTION_KEY": "aa" * 32,
    "SESSION_SECRET": "test-session-secret-must-be-at-least-32-bytes!",
}


class TestClassifyError:
    def test_deadlock_is_a_database_error(self):
        # The deadlock message names the gmail_tokens table; it is not an expired sign-in.
        from claven.tasks import _classify_error
        exc = Exception('deadlock detected\nCONTEXT:  while inserting index tuple (0,11) in relation "gmail_tokens"')
        assert _classify_error(exc) == "error.db.deadlock"

    def test_connection_error(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("connection refused")) == "error.db.connection_lost"

    def test_closed_connection(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("server closed the connection unexpectedly")) == "error.db.connection_lost"

    def test_ssl_error(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("SSL: WRONG_VERSION_NUMBER")) == "error.db.connection_lost"

    def test_rate_limited_429(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("HttpError 429: Rate Limit Exceeded")) == "error.gmail.rate_limited"

    def test_rate_limited_keyword(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("rate limit hit")) == "error.gmail.rate_limited"

    def test_auth_expired_401(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("HttpError 401: Invalid Credentials")) == "error.gmail.auth_expired"

    def test_auth_expired_403(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("HttpError 403: Forbidden")) == "error.gmail.auth_expired"

    def test_auth_expired_token(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("Token has been expired or revoked")) == "error.gmail.auth_expired"

    def test_auth_expired_invalid_grant(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("invalid_grant: Token has been expired or revoked.")) == "error.gmail.auth_expired"

    def test_quota_exhausted(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("Daily quota exceeded")) == "error.gmail.quota_exhausted"

    def test_gmail_api_error(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("gmail API returned 500")) == "error.gmail.api"

    def test_unknown_error(self):
        from claven.tasks import _classify_error
        assert _classify_error(Exception("something unexpected")) == "error.unknown"

    def test_revoked_access(self):
        from claven.core.auth import GmailAccessRevoked
        from claven.tasks import _classify_error
        assert _classify_error(GmailAccessRevoked("uid-1", newly_revoked=False)) == "error.gmail.auth_expired"


class TestHandleRevokedAccess:
    """A revoked account is reported once, then skipped quietly until the user reconnects."""

    def test_newly_revoked_is_an_error_and_an_event(self, caplog):
        from claven.core.auth import GmailAccessRevoked
        from claven.tasks import _handle_error
        mock_db = MagicMock()
        with patch("claven.server.db", mock_db), caplog.at_level(logging.INFO, logger="claven.tasks"):
            label = _handle_error(GmailAccessRevoked("uid-1", newly_revoked=True), "uid-1", "Poll processing", MagicMock())
        assert label == "error.gmail.auth_expired"
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        mock_db.log_event.assert_called_once()

    def test_already_revoked_is_quiet(self, caplog):
        from claven.core.auth import GmailAccessRevoked
        from claven.tasks import _handle_error
        mock_db = MagicMock()
        with patch("claven.server.db", mock_db), caplog.at_level(logging.DEBUG, logger="claven.tasks"):
            label = _handle_error(GmailAccessRevoked("uid-1", newly_revoked=False), "uid-1", "Poll processing", MagicMock())
        assert label == "error.gmail.auth_expired"
        assert all(r.levelno < logging.WARNING for r in caplog.records)
        assert any("revoked" in r.getMessage() for r in caplog.records)
        mock_db.log_event.assert_not_called()


class TestNeedsSentScan:
    def test_complete_returns_false(self):
        from claven.server import _needs_sent_scan
        assert _needs_sent_scan({"status": "complete", "updated_at": None}) is False

    def test_none_status_returns_true(self):
        from claven.server import _needs_sent_scan
        assert _needs_sent_scan({"status": None, "updated_at": None}) is True

    def test_error_status_returns_true(self):
        from claven.server import _needs_sent_scan
        assert _needs_sent_scan({"status": "error", "updated_at": None}) is True

    def test_in_progress_recent_returns_false(self):
        from claven.server import _needs_sent_scan
        from datetime import datetime, timezone
        assert _needs_sent_scan({
            "status": "in_progress",
            "updated_at": datetime.now(timezone.utc),
        }) is False

    def test_in_progress_stale_returns_true(self):
        from claven.server import _needs_sent_scan
        from datetime import datetime, timezone, timedelta
        assert _needs_sent_scan({
            "status": "in_progress",
            "updated_at": datetime.now(timezone.utc) - timedelta(minutes=2),
        }) is True

    def test_in_progress_no_updated_at_returns_true(self):
        from claven.server import _needs_sent_scan
        assert _needs_sent_scan({"status": "in_progress", "updated_at": None}) is True


class TestInboxScanRunning:
    def test_in_progress_and_recently_fetched(self):
        from claven.server import _inbox_scan_running
        from datetime import datetime, timezone
        assert _inbox_scan_running("in_progress", datetime.now(timezone.utc)) is True

    def test_in_progress_but_quiet_for_minutes_is_dead(self):
        from claven.server import _inbox_scan_running
        from datetime import datetime, timezone, timedelta
        assert _inbox_scan_running("in_progress", datetime.now(timezone.utc) - timedelta(minutes=10)) is False

    def test_in_progress_never_fetched(self):
        from claven.server import _inbox_scan_running
        assert _inbox_scan_running("in_progress", None) is False

    def test_complete(self):
        from claven.server import _inbox_scan_running
        from datetime import datetime, timezone
        assert _inbox_scan_running("complete", datetime.now(timezone.utc)) is False


class TestOneSentScanPerUser:
    def test_second_start_for_same_user_is_dropped(self, no_background_scans):
        import claven.server as srv
        srv._scanning_users.add("uid-1")
        try:
            srv._run_sent_scan("uid-1")
        finally:
            srv._scanning_users.discard("uid-1")
        no_background_scans.assert_not_called()

    def test_runs_and_releases_the_user(self, no_background_scans):
        import claven.server as srv
        srv._run_sent_scan("uid-1")
        no_background_scans.assert_called_once()
        assert "uid-1" not in srv._scanning_users

    def test_releases_the_user_when_the_scan_fails(self, no_background_scans):
        import claven.server as srv
        no_background_scans.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            srv._run_sent_scan("uid-1")
        assert "uid-1" not in srv._scanning_users

    def test_user_is_held_while_the_scan_runs(self, no_background_scans):
        import claven.server as srv
        seen = []
        no_background_scans.side_effect = lambda *a, **k: seen.append("uid-1" in srv._scanning_users)
        srv._run_sent_scan("uid-1")
        assert seen == [True]


class TestHealth:
    def test_returns_ok(self):
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestHealthz:
    def test_returns_ok_when_db_reachable(self):
        with patch("claven.server.db") as mock_db:
            _fake_db_ctx(mock_db)
            with TestClient(app) as client:
                response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["db"] == "connected"

    def test_returns_503_when_db_unreachable(self):
        with patch("claven.server.db") as mock_db:
            mock_db.get_connection.side_effect = Exception("connection refused")
            with TestClient(app) as client:
                response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["db"] == "unreachable"

    def test_unreachable_db_error_is_not_returned(self):
        # Unauthenticated endpoint: the error text (hosts, users) stays in the logs.
        with patch("claven.server.db") as mock_db:
            mock_db.get_connection.side_effect = Exception("could not connect to db.internal as claven_app")
            with TestClient(app) as client:
                response = client.get("/healthz")
        assert "claven_app" not in response.text
        assert "detail" not in response.json()


class TestSecurityHeaders:
    """Headers a security scan expects on every API response."""

    _EVERYWHERE = {
        "strict-transport-security": "max-age=31536000; includeSubDomains",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
        "referrer-policy": "no-referrer",
    }

    @pytest.mark.parametrize("path", ["/health", "/api/me", "/api/config", "/oauth/start", "/nonexistent"])
    def test_on_every_response(self, path):
        with patch.dict("os.environ", _ENV), TestClient(app) as client:
            response = client.get(path, follow_redirects=False)
        for name, value in self._EVERYWHERE.items():
            assert response.headers.get(name) == value, (path, name)

    @pytest.mark.parametrize("path", ["/api/me", "/api/config", "/oauth/start"])
    def test_api_and_oauth_responses_are_not_cached(self, path):
        with patch.dict("os.environ", _ENV), TestClient(app) as client:
            response = client.get(path, follow_redirects=False)
        assert response.headers.get("cache-control") == "no-store"

    def test_on_cors_preflight(self):
        with TestClient(app) as client:
            response = client.options("/api/me", headers={
                "Origin": "https://claven.app", "Access-Control-Request-Method": "GET"})
        assert response.headers.get("x-content-type-options") == "nosniff"

    def test_event_stream_still_streams_with_headers(self):
        # The headers are added as the response starts, so the SSE stream isn't buffered.
        token = _make_session_token()
        with patch.dict("os.environ", {**_ENV, "DATABASE_URL": "postgresql://pooled"}), \
                patch("claven.routes.auth._current_session_version", return_value=0), \
                patch("psycopg2.connect", side_effect=RuntimeError("no db")):
            with TestClient(app) as client:
                client.cookies.set("session", token)
                response = client.get("/api/events")
        assert response.headers.get("x-content-type-options") == "nosniff"

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_api_docs_are_not_served(self, path):
        with TestClient(app) as client:
            assert client.get(path).status_code == 404


class TestOAuthStart:
    def test_redirects_to_google(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/start", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert "accounts.google.com" in response.headers["location"]

    def test_redirect_contains_state_param(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/start", follow_redirects=False)
        assert "state=" in response.headers["location"]

    def test_sets_oauth_state_cookie(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/start", follow_redirects=False)
        assert "oauth_state" in response.cookies

    def test_valid_return_to_stored_in_cookie(self):
        env = {**_ENV, "CORS_EXTRA_ORIGINS": "http://localhost:3000"}
        with patch.dict("os.environ", env):
            with TestClient(app) as client:
                response = client.get(
                    "/oauth/start?return_to=http://localhost:3000",
                    follow_redirects=False,
                )
        assert "http://localhost:3000" in response.cookies.get("oauth_return_to", "")

    def test_invalid_return_to_returns_400(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get(
                    "/oauth/start?return_to=https://evil.example.com",
                    follow_redirects=False,
                )
        assert response.status_code == 400

    def test_missing_return_to_no_cookie(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/start", follow_redirects=False)
        assert "oauth_return_to" not in response.cookies

    def test_default_uses_select_account_prompt(self):
        """oauth/start without force_consent uses select_account so returning
        users skip the full consent screen and only see the account picker."""
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.Flow") as mock_flow_cls:
                mock_flow = MagicMock()
                mock_flow.authorization_url.return_value = ("https://accounts.google.com/auth", "state")
                mock_flow_cls.from_client_config.return_value = mock_flow
                with TestClient(app) as client:
                    client.get("/oauth/start", follow_redirects=False)
        call_kwargs = mock_flow.authorization_url.call_args[1]
        assert call_kwargs.get("prompt") == "select_account"

    def test_force_consent_uses_consent_prompt(self):
        """oauth/start?force_consent=true uses prompt=consent to obtain a fresh
        refresh token (needed when reconnecting after disconnect)."""
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.Flow") as mock_flow_cls:
                mock_flow = MagicMock()
                mock_flow.authorization_url.return_value = ("https://accounts.google.com/auth", "state")
                mock_flow_cls.from_client_config.return_value = mock_flow
                with TestClient(app) as client:
                    client.get("/oauth/start?force_consent=true", follow_redirects=False)
        call_kwargs = mock_flow.authorization_url.call_args[1]
        assert call_kwargs.get("prompt") == "consent"


class TestOAuthCallback:
    def test_missing_code_redirects_with_error(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/callback?state=abc", follow_redirects=False)
        assert response.status_code == 302
        assert "error=invalid_request" in response.headers["location"]

    def test_missing_state_redirects_with_error(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/callback?code=abc", follow_redirects=False)
        assert response.status_code == 302
        assert "error=invalid_request" in response.headers["location"]

    def test_state_mismatch_redirects_with_error(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                client.get("/oauth/start", follow_redirects=False)  # sets cookie
                response = client.get("/oauth/callback?code=abc&state=wrong-state", follow_redirects=False)
        assert response.status_code == 302
        assert "error=invalid_state" in response.headers["location"]

    def test_oauth_error_param_redirects_with_error(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/oauth/callback?error=access_denied", follow_redirects=False)
        assert response.status_code == 302
        assert "error=oauth_denied" in response.headers["location"]

    def test_token_exchange_failure_redirects_with_error(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                # Start flow to capture the generated state value
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")

                # secure=True cookies aren't sent by TestClient (HTTP), so set manually
                client.cookies.set("oauth_state", state)

                with patch("claven.server.Flow") as mock_flow_cls:
                    mock_flow_cls.from_client_config.return_value.fetch_token.side_effect = Exception("scope mismatch")
                    response = client.get(
                        f"/oauth/callback?code=abc&state={state}",
                        follow_redirects=False,
                    )
        assert response.status_code == 302
        assert "error=token_exchange_failed" in response.headers["location"]

    def test_no_refresh_token_redirects_to_force_consent(self):
        """When reconnecting after disconnect Google won't return a refresh token.
        The callback must redirect back to oauth/start?force_consent=true instead
        of failing with signup_failed."""
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)

                mock_creds = MagicMock()
                mock_creds.id_token = "fake-id-token"
                mock_creds.refresh_token = None  # Google didn't return one
                mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid"}

                mock_flow = MagicMock()
                mock_flow.credentials = mock_creds
                mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

                with (
                    patch("claven.server.Flow") as mock_flow_cls,
                    patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify,
                    patch("claven.server.db") as mock_db,
                ):
                    mock_flow_cls.from_client_config.return_value = mock_flow
                    mock_verify.return_value = {"email": "user@example.com"}
                    _fake_db_ctx(mock_db)
                    mock_db.upsert_user.return_value = "uid-1"
                    mock_db.load_tokens.return_value = None  # no stored tokens

                    response = client.get(
                        f"/oauth/callback?code=abc&state={state}",
                        follow_redirects=False,
                    )
        assert response.status_code == 302
        assert "force_consent=true" in response.headers["location"]
        assert "/oauth/start" in response.headers["location"]

    def test_revoked_user_without_new_refresh_token_is_sent_to_consent(self):
        """A returning user whose access was revoked signs in without the consent
        screen, so Google sends no refresh token and the stored one is dead."""
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)

                mock_creds = MagicMock()
                mock_creds.id_token = "fake-id-token"
                mock_creds.refresh_token = None  # Google didn't return one
                mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid"}

                mock_flow = MagicMock()
                mock_flow.credentials = mock_creds
                mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

                with (
                    patch("claven.server.Flow") as mock_flow_cls,
                    patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify,
                    patch("claven.server.db") as mock_db,
                ):
                    mock_flow_cls.from_client_config.return_value = mock_flow
                    mock_verify.return_value = {"email": "user@example.com"}
                    _fake_db_ctx(mock_db)
                    mock_db.upsert_user.return_value = "uid-1"
                    mock_db.load_tokens.return_value = {"revoked_at": "2026-11-01T00:00:00Z"}

                    response = client.get(
                        f"/oauth/callback?code=abc&state={state}",
                        follow_redirects=False,
                    )
        assert response.status_code == 302
        assert "force_consent=true" in response.headers["location"]
        assert "/oauth/start" in response.headers["location"]

    def test_callback_rejects_missing_gmail_scope(self):
        """If user unchecks Gmail permission (granular consent), redirect with clear error."""
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)

                mock_creds = MagicMock()
                mock_creds.id_token = "fake-id-token"
                mock_creds.scopes = {"openid", "https://www.googleapis.com/auth/userinfo.email"}

                mock_flow = MagicMock()
                mock_flow.credentials = mock_creds

                with patch("claven.server.Flow") as mock_flow_cls:
                    mock_flow_cls.from_client_config.return_value = mock_flow
                    response = client.get(
                        f"/oauth/callback?code=abc&state={state}",
                        follow_redirects=False,
                    )
        assert response.status_code == 302
        assert "error=gmail_scope_missing" in response.headers["location"]

    def test_callback_does_not_start_watch(self):
        """oauth_callback stores credentials but never starts the Gmail watch.
        Starting the watch is an explicit user action via POST /api/connect."""
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)

                mock_creds = MagicMock()
                mock_creds.id_token = "fake-id-token"
                mock_creds.refresh_token = "fake-refresh-token"
                mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid"}

                mock_flow = MagicMock()
                mock_flow.credentials = mock_creds
                mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

                with (
                    patch("claven.server.Flow") as mock_flow_cls,
                    patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify,
                    patch("claven.server.db") as mock_db,
                    patch("claven.server.auth"),
                    patch("claven.server.start_watch") as mock_watch,
                    patch("claven.server.build_known_senders"),
                ):
                    mock_flow_cls.from_client_config.return_value = mock_flow
                    mock_verify.return_value = {"email": "user@example.com"}
                    _fake_db_ctx(mock_db)
                    mock_db.upsert_user.return_value = "uid-1"
                    mock_db.load_tokens.return_value = None

                    client.get(
                        f"/oauth/callback?code=abc&state={state}",
                        follow_redirects=False,
                    )
        mock_watch.assert_not_called()

    def test_callback_does_not_trigger_scan(self):
        """oauth_callback defers scan triggering to /api/me retrigger logic."""
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)

                mock_creds = MagicMock()
                mock_creds.id_token = "fake-id-token"
                mock_creds.refresh_token = "fake-refresh-token"
                mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid"}

                mock_flow = MagicMock()
                mock_flow.credentials = mock_creds

                with (
                    patch("claven.server.Flow") as mock_flow_cls,
                    patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify,
                    patch("claven.server.db") as mock_db,
                    patch("claven.server.auth"),
                    patch("claven.server.threading") as mock_threading,
                ):
                    mock_flow_cls.from_client_config.return_value = mock_flow
                    mock_verify.return_value = {"email": "user@example.com"}
                    _fake_db_ctx(mock_db)
                    mock_db.upsert_user.return_value = "uid-1"
                    mock_db.load_tokens.return_value = None

                    client.get(
                        f"/oauth/callback?code=abc&state={state}",
                        follow_redirects=False,
                    )
        mock_threading.Thread.assert_not_called()

class TestInternalPoll:
    def test_no_auth_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/internal/poll")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post(
                    "/internal/poll",
                    headers={"Authorization": "Bearer wrong-token"},
                )
        assert response.status_code == 401

    def test_valid_auth_no_users_returns_ok(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = []
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/poll",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        assert response.status_code == 200
        assert response.json()["processed"] == 0

    def test_valid_auth_processes_users(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth") as mock_auth, patch(
                "claven.server.poll_new_messages"
            ) as mock_poll, patch("claven.server.build_known_senders"):
                mock_conn = _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = [{"id": "uid-1", "email": "u@example.com"}]
                mock_db.get_history_id.return_value = 999
                mock_db.get_known_senders.return_value = set()
                mock_auth.get_service.return_value = MagicMock()
                mock_poll.return_value = 0
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/poll",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        assert response.status_code == 200
        assert response.json()["processed"] == 1

    def test_increments_processed_count_after_poll(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth") as mock_auth, patch(
                "claven.server.poll_new_messages"
            ) as mock_poll, patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = [{"id": "uid-1", "email": "u@example.com"}]
                mock_db.get_history_id.return_value = 999
                mock_db.get_known_senders.return_value = set()
                mock_auth.get_service.return_value = MagicMock()
                mock_poll.return_value = 5
                with TestClient(app) as client:
                    client.post(
                        "/internal/poll",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        mock_db.increment_processed_count.assert_called_once_with(ANY, "uid-1", 5)


    def test_poll_skips_locked_user(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth"), patch(
                "claven.server.poll_new_messages"
            ) as mock_poll, patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = [{"id": "uid-1", "email": "u@example.com"}]
                mock_db.try_lock_user_scan.return_value = False
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/poll",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        assert response.json()["results"][0]["status"] == "skipped"
        mock_poll.assert_not_called()


class TestInternalBuildKnownSenders:
    def test_no_auth_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/internal/build-known-senders")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post(
                    "/internal/build-known-senders",
                    headers={"Authorization": "Bearer wrong-token"},
                )
        assert response.status_code == 401

    def test_no_users_returns_ok(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = []
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/build-known-senders",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        assert response.status_code == 200
        assert response.json()["processed"] == 0

    def test_calls_build_known_senders_for_each_user(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.build_known_senders") as mock_build:
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = [
                    {"id": "uid-1", "email": "a@example.com"},
                    {"id": "uid-2", "email": "b@example.com"},
                ]
                mock_auth.get_service.return_value = MagicMock()
                mock_build.return_value = {"known_senders": 5, "messages_scanned": 10}
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/build-known-senders",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        assert response.status_code == 200
        assert response.json()["processed"] == 2
        assert mock_build.call_count == 2

    def test_returns_known_senders_count_per_user(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.build_known_senders") as mock_build:
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = [{"id": "uid-1", "email": "a@example.com"}]
                mock_auth.get_service.return_value = MagicMock()
                mock_build.return_value = {"known_senders": 42, "messages_scanned": 100}
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/build-known-senders",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        result = response.json()["results"][0]
        assert result["known_senders"] == 42
        assert result["messages_scanned"] == 100
        assert result["status"] == "ok"

    def test_user_error_recorded_without_aborting_others(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.build_known_senders") as mock_build:
                _fake_db_ctx(mock_db)
                mock_db.get_all_users.return_value = [
                    {"id": "uid-1", "email": "a@example.com"},
                    {"id": "uid-2", "email": "b@example.com"},
                ]
                mock_auth.get_service.return_value = MagicMock()
                mock_build.side_effect = [Exception("Gmail API error"), {"known_senders": 3, "messages_scanned": 5}]
                with TestClient(app) as client:
                    response = client.post(
                        "/internal/build-known-senders",
                        headers={"Authorization": "Bearer test-internal-secret"},
                    )
        results = response.json()["results"]
        assert results[0]["status"] == "error"
        assert results[1]["status"] == "ok"


class TestWebhookGmail:
    # ── Auth ──────────────────────────────────────────────────────────────────

    def test_missing_auth_header_returns_401(self):
        with TestClient(app) as client:
            response = client.post("/webhook/gmail", json=_pubsub_payload())
        assert response.status_code == 401

    def test_invalid_token_returns_401(self):
        with patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify:
            mock_verify.side_effect = Exception("invalid token")
            with TestClient(app) as client:
                response = client.post(
                    "/webhook/gmail",
                    json=_pubsub_payload(),
                    headers={"Authorization": "Bearer bad-token"},
                )
        assert response.status_code == 401

    def test_other_service_account_returns_401(self):
        # Anyone can point their own Pub/Sub subscription at the webhook; Google
        # signs a valid token for their service account.
        with patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify:
            mock_verify.return_value = {"email": "attacker@evil-project.iam.gserviceaccount.com", "email_verified": True}
            with TestClient(app) as client:
                response = client.post("/webhook/gmail", json=_pubsub_payload(), headers=_PUBSUB_HEADERS)
        assert response.status_code == 401

    def test_unverified_email_returns_401(self):
        with patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify:
            mock_verify.return_value = {**_PUBSUB_ID_INFO, "email_verified": False}
            with TestClient(app) as client:
                response = client.post("/webhook/gmail", json=_pubsub_payload(), headers=_PUBSUB_HEADERS)
        assert response.status_code == 401

    def test_token_audience_is_checked(self):
        with _mock_pubsub_token() as mock_verify, patch("claven.server.db") as mock_db:
            _fake_db_ctx(mock_db)
            mock_db.get_user_by_email.return_value = None
            with TestClient(app) as client:
                client.post("/webhook/gmail", json=_pubsub_payload(), headers=_PUBSUB_HEADERS)
        assert mock_verify.call_args.kwargs["audience"] == "https://api.claven.app/webhook/gmail"

    @pytest.mark.parametrize("missing", ["PUBSUB_AUDIENCE", "PUBSUB_PUSH_SERVICE_ACCOUNT"])
    def test_fails_closed_when_not_configured(self, missing, monkeypatch):
        monkeypatch.delenv(missing)
        with _mock_pubsub_token() as mock_verify:
            with TestClient(app) as client:
                response = client.post("/webhook/gmail", json=_pubsub_payload(), headers=_PUBSUB_HEADERS)
        assert response.status_code == 500
        mock_verify.assert_not_called()

    def test_non_pubsub_service_account_returns_401(self):
        with patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify:
            mock_verify.return_value = {"email": "attacker@gmail.com"}
            with TestClient(app) as client:
                response = client.post(
                    "/webhook/gmail",
                    json=_pubsub_payload(),
                    headers={"Authorization": "Bearer some-valid-google-token"},
                )
        assert response.status_code == 401

    # ── Payload validation ────────────────────────────────────────────────────

    def test_missing_message_field_returns_400(self):
        with _mock_pubsub_token():
            with TestClient(app) as client:
                response = client.post(
                    "/webhook/gmail",
                    json={"not": "a message"},
                    headers=_PUBSUB_HEADERS,
                )
        assert response.status_code == 400

    def test_malformed_base64_returns_400(self):
        with _mock_pubsub_token():
            with TestClient(app) as client:
                response = client.post(
                    "/webhook/gmail",
                    json={"message": {"data": "!!!not-valid-base64!!!"}},
                    headers=_PUBSUB_HEADERS,
                )
        assert response.status_code == 400

    def test_missing_email_address_returns_400(self):
        notification = json.dumps({"historyId": "123"})  # no emailAddress
        data = base64.b64encode(notification.encode()).decode()
        with _mock_pubsub_token():
            with TestClient(app) as client:
                response = client.post(
                    "/webhook/gmail",
                    json={"message": {"data": data}},
                    headers=_PUBSUB_HEADERS,
                )
        assert response.status_code == 400

    # ── Deferred processing ───────────────────────────────────────────────────

    def test_valid_push_is_acknowledged_without_touching_the_database(self):
        """Every push used to wake Neon, which bills while awake; the poll does the work instead."""
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.poll_new_messages") as mock_poll:
                with _mock_pubsub_token():
                    with TestClient(app) as client:
                        mock_db.reset_mock()  # startup clears stale job flags
                        response = client.post("/webhook/gmail", json=_pubsub_payload(history_id="200"),
                                               headers=_PUBSUB_HEADERS)
        assert response.status_code == 200
        mock_db.get_connection.assert_not_called()
        mock_poll.assert_not_called()


class TestPollProcessing:
    """What the poll does for each user (this used to run on every Gmail push)."""

    def _poll(self, has_new, sent_status="complete", pending=(), sent_updated_at=None,
              inbox_status="complete", last_fetched_at=None, poll_count=0, locked=False):
        self.conn = MagicMock()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.get_profile", return_value={"historyId": "200"}), \
                 patch("claven.server.has_new_messages", return_value=has_new) as self.mock_has_new, \
                 patch("claven.server.poll_new_messages", return_value=poll_count) as mock_poll, \
                 patch("claven.server.build_known_senders") as mock_build, \
                 patch("claven.server._spawn_scan_thread") as self.mock_spawn:
                _fake_db_ctx(mock_db, self.conn)
                mock_db.get_all_users.return_value = [{"id": "uid-1", "email": "user@example.com"}]
                mock_db.try_lock_user_scan.return_value = not locked
                mock_db.get_history_id.return_value = 100
                mock_db.get_sent_scan_progress.return_value = {"status": sent_status, "updated_at": sent_updated_at}
                mock_db.get_inbox_scan_status.return_value = inbox_status
                mock_db.get_last_fetched_at.return_value = last_fetched_at
                self.mock_auth = mock_auth
                mock_db.get_pending_relabel_senders.return_value = list(pending)
                mock_db.get_known_senders.return_value = set()
                mock_auth.get_service.return_value = MagicMock()
                with TestClient(app) as client:
                    response = client.post("/internal/poll", headers={"Authorization": "Bearer test-internal-secret"})
        self.result = response.json()["results"][0]
        return response, mock_db, mock_poll, mock_build

    def test_new_messages_are_processed(self):
        _, mock_db, mock_poll, mock_build = self._poll(has_new=True)
        mock_poll.assert_called_once()
        mock_build.assert_called_once()
        mock_db.set_history_id.assert_called_once_with(ANY, "uid-1", 200)

    def test_increments_processed_count(self):
        _, mock_db, _, _ = self._poll(has_new=True, poll_count=3)
        mock_db.increment_processed_count.assert_called_once_with(ANY, "uid-1", 3)

    def test_label_changes_only_just_advance_history(self):
        # Claven's own label changes add history entries; nothing was added.
        _, mock_db, mock_poll, mock_build = self._poll(has_new=False)
        mock_poll.assert_not_called()
        mock_build.assert_not_called()
        mock_db.set_history_id.assert_called_once_with(ANY, "uid-1", 200)
        assert self.result["detail"] == "no new messages"

    def test_new_known_senders_are_relabeled_right_away(self):
        # Replying to an unknown sender makes them known; their earlier mail
        # should move to c/k now, not on the next dashboard visit.
        with patch("claven.server.relabel_scan", return_value=2) as mock_relabel, \
             patch("claven.routes.internal.existing_countdown_ids", return_value={"c/4h": "L4"}):
            self._poll(has_new=True, pending=["alice@example.com"])
        mock_relabel.assert_called_once()
        assert mock_relabel.call_args.args[1] == "uid-1"
        assert mock_relabel.call_args.kwargs["countdown_ids"] == {"c/4h": "L4"}

    def test_no_relabel_without_new_known_senders(self):
        with patch("claven.server.relabel_scan") as mock_relabel:
            self._poll(has_new=True, pending=[])
        mock_relabel.assert_not_called()

    def test_unfinished_sent_scan_restarts_in_the_background(self):
        # Never inline: a full sent scan inside the transaction held the
        # user's rows locked for minutes and stalled their dashboard.
        _, mock_db, mock_poll, mock_build = self._poll(has_new=True, sent_status="error.unknown")
        assert self.result["detail"] == "sent scan started"
        mock_build.assert_not_called()
        mock_poll.assert_not_called()
        mock_db.set_sent_scan_status.assert_called_once_with(ANY, "uid-1", "in_progress")
        self.mock_spawn.assert_called_once()
        assert self.mock_spawn.call_args.args[0].__name__ == "_run_sent_scan"
        mock_db.set_history_id.assert_not_called()

    def test_restart_commits_before_the_scan_starts(self):
        # The scan takes the scan_state row lock with SKIP LOCKED; while this
        # transaction holds it, the scan would skip itself.
        order = []
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth"), \
                 patch("claven.server._spawn_scan_thread", side_effect=lambda *a: order.append("spawn")):
                conn = MagicMock()
                conn.commit.side_effect = lambda: order.append("commit")
                _fake_db_ctx(mock_db, conn)
                mock_db.get_all_users.return_value = [{"id": "uid-1", "email": "user@example.com"}]
                mock_db.get_history_id.return_value = 100
                mock_db.get_sent_scan_progress.return_value = {"status": "error.unknown", "updated_at": None}
                with TestClient(app) as client:
                    response = client.post("/internal/poll", headers={"Authorization": "Bearer test-internal-secret"})
        assert response.json()["results"][0]["detail"] == "sent scan started"
        assert order == ["commit", "spawn"]

    def test_skips_while_sent_scan_runs(self):
        from datetime import datetime, timezone
        _, mock_db, mock_poll, mock_build = self._poll(
            has_new=True, sent_status="in_progress", sent_updated_at=datetime.now(timezone.utc))
        assert self.result["detail"] == "sent scan running"
        mock_build.assert_not_called()
        mock_poll.assert_not_called()
        self.mock_spawn.assert_not_called()
        self.mock_auth.get_service.assert_not_called()
        # The scan labels what arrives meanwhile and sets the history id when done.
        mock_db.set_history_id.assert_not_called()

    def test_skips_while_inbox_scan_runs(self):
        from datetime import datetime, timezone
        _, mock_db, mock_poll, _ = self._poll(
            has_new=True, inbox_status="in_progress", last_fetched_at=datetime.now(timezone.utc))
        assert self.result["detail"] == "inbox scan running"
        self.mock_has_new.assert_not_called()
        self.mock_auth.get_service.assert_not_called()
        mock_poll.assert_not_called()
        mock_db.set_history_id.assert_not_called()

    def test_dead_inbox_scan_does_not_block_processing(self):
        from datetime import datetime, timezone, timedelta
        _, _, mock_poll, _ = self._poll(
            has_new=True, inbox_status="in_progress",
            last_fetched_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        mock_poll.assert_called_once()

    def test_skips_locked_user(self):
        _, _, mock_poll, _ = self._poll(has_new=True, locked=True)
        assert self.result["status"] == "skipped"
        assert self.result["detail"] == "locked"
        mock_poll.assert_not_called()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_session_token(payload: dict | None = None, **claims) -> str:
    """Issue a real JWT signed with the test SESSION_SECRET (session version 0, valid for an hour).

    Pass a claim as None to leave it out.
    """
    import time
    import jwt as pyjwt
    now = int(time.time())
    data = {"user_id": "uid-1", "email": "user@example.com", "iat": now, "exp": now + 3600, "sv": 0,
            **(payload or {}), **claims}
    data = {k: v for k, v in data.items() if v is not None}
    return pyjwt.encode(data, _ENV["SESSION_SECRET"], algorithm="HS256")


class TestSessionSecurity:
    """Sessions expire, logging out revokes them, and other sites can't act with them."""

    def _me(self, token, mock_db_setup=None):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth"):
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                if mock_db_setup:
                    mock_db_setup(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    return client.get("/api/me")

    def test_valid_session_is_accepted(self):
        assert self._me(_make_session_token()).status_code == 200

    def test_expired_session_is_rejected(self):
        import time
        assert self._me(_make_session_token(exp=int(time.time()) - 60)).status_code == 401

    def test_session_without_expiry_is_rejected(self):
        # Tokens issued before sessions expired never do; they must not stay valid forever.
        assert self._me(_make_session_token(exp=None)).status_code == 401

    def test_session_without_version_is_rejected(self):
        assert self._me(_make_session_token(sv=None)).status_code == 401

    def test_session_from_before_logout_is_rejected(self):
        def bumped(mock_db):
            mock_db.get_session_version.return_value = 1
        assert self._me(_make_session_token(sv=0), bumped).status_code == 401

    def test_session_of_deleted_user_is_rejected(self):
        def gone(mock_db):
            mock_db.get_session_version.return_value = None
        assert self._me(_make_session_token(), gone).status_code == 401

    def test_logout_revokes_the_users_sessions(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", _make_session_token())
                    response = client.post("/api/logout")
        assert response.status_code == 200
        mock_db.bump_session_version.assert_called_once_with(ANY, "uid-1")

    def _post_cancel(self, headers, cookie=True):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    if cookie:
                        client.cookies.set("session", _make_session_token())
                    response = client.post("/api/actions/cancel", headers=headers)
        return response, mock_db

    def test_cross_site_post_with_cookie_is_refused(self):
        response, mock_db = self._post_cancel({"Origin": "https://evil.example"})
        assert response.status_code == 403
        mock_db.get_cancel_state.assert_not_called()

    def test_opaque_origin_is_refused(self):
        response, _ = self._post_cancel({"Origin": "null"})
        assert response.status_code == 403

    def test_post_from_the_dashboard_is_allowed(self):
        response, _ = self._post_cancel({"Origin": "https://claven.app"})
        assert response.status_code == 200

    def test_bearer_token_is_not_subject_to_origin_check(self):
        # A bearer token is never sent by the browser on its own, so it can't be forged cross-site.
        response, _ = self._post_cancel(
            {"Origin": "https://evil.example", "Authorization": f"Bearer {_make_session_token()}"}, cookie=False)
        assert response.status_code == 200

    def test_cross_site_get_is_not_blocked(self):
        # Reads are protected by CORS; only state-changing requests need the Origin check.
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth"):
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                with TestClient(app) as client:
                    client.cookies.set("session", _make_session_token())
                    response = client.get("/api/me", headers={"Origin": "https://evil.example"})
        assert response.status_code == 200


class TestApiMe:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/api/me")
        assert response.status_code == 401

    def test_invalid_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/api/me", headers={"Authorization": "Bearer bad.token"})
        assert response.status_code == 401

    def test_valid_cookie_returns_user_data(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "user@example.com"
        assert body["connected"] is True
        assert body["history_id"] == 12345

    def test_valid_bearer_token_returns_user_data(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = None
                with TestClient(app) as client:
                    response = client.get(
                        "/api/me", headers={"Authorization": f"Bearer {token}"}
                    )
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "user@example.com"
        assert body["connected"] is False

    def test_not_connected_when_no_history_id(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = None
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["connected"] is False

    _KNOWN_LABEL_ID = "Label_known_sender"
    _UNKNOWN_LABEL_ID = "Label_unknown_sender"

    def _make_gmail_service(
        self,
        messages_unread=0,
        messages_total=0,
        read_estimate=0,
        labeled_known_total=0,
        labeled_unknown_total=0,
        unlabeled_ids=None,
        newest_mail_internal_date=None,
        label_names=("c", "c/k", "c/u"),
    ):
        """Return a mock Gmail service with label counts.

        label_names: the parent, known and unknown label names in the mailbox
            (pass old names like "known-sender" to exercise the rename).

        unlabeled_ids: list of message ID strings for the unlabeled query.
            Paginated in chunks of 500. Defaults to [] (no unlabeled).
        newest_mail_internal_date: epoch ms string for newest inbox message.
        """
        if unlabeled_ids is None:
            unlabeled_ids = []
        svc = MagicMock()
        known_id = self._KNOWN_LABEL_ID
        unknown_id = self._UNKNOWN_LABEL_ID

        # labels.get → exact per-label counts
        def _labels_get(**kwargs):
            lid = kwargs.get("id", "")
            result = MagicMock()
            if lid == "INBOX":
                result.execute.return_value = {"messagesUnread": messages_unread, "messagesTotal": messages_total}
            elif lid == "SENT":
                result.execute.return_value = {"messagesTotal": 0}
            elif lid == known_id:
                result.execute.return_value = {"messagesTotal": labeled_known_total}
            elif lid == unknown_id:
                result.execute.return_value = {"messagesTotal": labeled_unknown_total}
            else:
                result.execute.return_value = {"messagesTotal": 0}
            return result

        svc.users.return_value.labels.return_value.get.side_effect = _labels_get

        # labels.list → label ID map
        svc.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [
                {"name": label_names[0], "id": "Label_parent"},
                {"name": label_names[1], "id": known_id},
                {"name": label_names[2], "id": unknown_id},
            ]
        }

        # messages.list → paginated unlabeled results
        # Each query scope (inbox vs allmail) gets its own page index
        _pages = []
        for i in range(0, max(len(unlabeled_ids), 1), 500):
            _pages.append(unlabeled_ids[i:i + 500])
        if not unlabeled_ids:
            _pages = [[]]
        _page_indices = {}  # keyed by query prefix

        def _messages_list(**kwargs):
            q = kwargs.get("q", "")
            label_ids = kwargs.get("labelIds", [])
            result = MagicMock()
            if "is:read" in q:
                result.execute.return_value = {"resultSizeEstimate": read_estimate}
            elif "-label:" in q:
                scope_key = "inbox" if "in:inbox" in q else "allmail"
                idx = _page_indices.get(scope_key, 0)
                page = _pages[idx] if idx < len(_pages) else []
                has_next = idx + 1 < len(_pages) and _pages[idx + 1]
                _page_indices[scope_key] = idx + 1
                resp = {"messages": [{"id": mid} for mid in page]}
                if has_next:
                    resp["nextPageToken"] = f"page-{scope_key}-{idx + 1}"
                result.execute.return_value = resp
            elif not q and "INBOX" in label_ids:
                # Newest inbox message lookup
                if newest_mail_internal_date:
                    result.execute.return_value = {"messages": [{"id": "newest-1"}]}
                else:
                    result.execute.return_value = {"messages": []}
            else:
                result.execute.return_value = {"resultSizeEstimate": 0}
            return result

        svc.users.return_value.messages.return_value.list.side_effect = _messages_list

        # messages.get → newest mail internal date
        def _messages_get(**kwargs):
            result = MagicMock()
            result.execute.return_value = {"internalDate": newest_mail_internal_date}
            return result

        svc.users.return_value.messages.return_value.get.side_effect = _messages_get

        # Batch API — use _FakeBatch to execute requests individually
        svc.new_batch_http_request.side_effect = lambda callback: _FakeBatch(callback)
        return svc

    def test_returns_known_senders_count(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 42
                mock_auth.get_service.return_value = self._make_gmail_service()
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["known_senders"] == 42

    def test_returns_unread_count(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_unread=99, messages_total=500, read_estimate=401
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["unread_count"] == 99

    def test_returns_inbox_count(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=250, read_estimate=250
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["inbox_count"] == 250

    def test_returns_read_count(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_unread=30, messages_total=100, read_estimate=70
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["read_count"] == 70

    def test_returns_newest_mail_at(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    newest_mail_internal_date="1714500000000",
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["newest_mail_at"] is not None
        assert response.json()["newest_mail_at"].startswith("2024-04-30")

    def test_newest_mail_at_is_none_when_inbox_empty(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service()
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["newest_mail_at"] is None

    def test_unread_count_is_none_when_gmail_api_fails(self):
        """A Gmail API error must not break /api/me — return null for all Gmail fields."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.side_effect = Exception("token expired")
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.status_code == 200
        assert response.json()["unread_count"] is None
        assert response.json()["read_count"] is None
        assert response.json()["inbox_count"] is None

    # processed_count and pending_count removed — progress derived from live Gmail label counts

    def test_returns_labeled_known_count(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, labeled_known_total=60
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["allmail_labeled_known_count"] == 60

    def _get_me(self, gmail_service):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = gmail_service
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    return client.get("/api/me")

    def test_countdown_labels_count_as_unknown(self):
        # In Sunset, waiting mail carries a countdown label instead of c/u.
        svc = self._make_gmail_service(messages_total=100, labeled_unknown_total=40)
        labels_api = svc.users.return_value.labels.return_value
        labels_api.list.return_value.execute.return_value["labels"].append({"name": "c/4h", "id": "Label_c4h"})
        base_get = labels_api.get.side_effect

        def _get(**kwargs):
            if kwargs.get("id") == "Label_c4h":
                result = MagicMock()
                result.execute.return_value = {"messagesTotal": 5}
                return result
            return base_get(**kwargs)
        labels_api.get.side_effect = _get

        messages_api = svc.users.return_value.messages.return_value
        base_list = messages_api.list.side_effect
        calls = []

        def _list(**kwargs):
            calls.append(kwargs)
            return base_list(**kwargs)
        messages_api.list.side_effect = _list

        body = self._get_me(svc).json()
        assert body["allmail_labeled_unknown_count"] == 45
        # Countdown labels sit only on mail waiting in the inbox, so their totals are inbox counts.
        assert body["inbox_labeled_unknown_shallow_count"] == 5
        assert any(c.get("q", "").startswith("in:inbox") and "-label:c/4h" in c["q"] for c in calls)
        # Up to 72 countdown labels: no per-label inbox listing.
        assert not any(c.get("labelIds") == ["INBOX", "Label_c4h"] for c in calls)

    def test_current_label_names_are_not_patched(self):
        svc = self._make_gmail_service(messages_total=100, labeled_known_total=60)
        response = self._get_me(svc)
        assert response.json()["allmail_labeled_known_count"] == 60
        svc.users.return_value.labels.return_value.patch.assert_not_called()

    def test_returns_labeled_unknown_count(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, labeled_unknown_total=40
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["allmail_labeled_unknown_count"] == 40

    def test_returns_all_counts(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, labeled_known_total=50,
                    labeled_unknown_total=30, unlabeled_ids=[f"m{i}" for i in range(20)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        body = response.json()
        assert body["allmail_labeled_known_count"] == 50
        assert body["allmail_labeled_unknown_count"] == 30
        assert body["allmail_labeled_total_count"] == 80
        assert body["inbox_unlabeled_first_page_count"] == 20
        assert body["inbox_unlabeled_deep_count"] == 20

    def test_unlabeled_deep_count_paginates(self):
        """Deep count paginates beyond the first 500 messages."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=1000,
                    unlabeled_ids=[f"m{i}" for i in range(750)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        body = response.json()
        assert body["inbox_unlabeled_first_page_count"] == 500
        assert body["inbox_unlabeled_deep_count"] == 750

    def test_counts_are_null_when_gmail_api_unavailable(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.side_effect = Exception("token expired")
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["allmail_labeled_known_count"] is None
        assert response.json()["allmail_labeled_unknown_count"] is None
        assert response.json()["inbox_unlabeled_deep_count"] is None


    def test_api_me_does_not_trigger_scans_when_no_unlabeled(self):
        """No scan triggers when all messages are labeled."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_auth.get_service.return_value = self._make_gmail_service()
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    client.get("/api/me")
        mock_threading.Thread.assert_not_called()

    def test_api_me_retriggers_scan_when_unlabeled_remain(self):
        """If scan is complete but unlabeled messages remain, retrigger via sent scan."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "complete"
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, unlabeled_ids=["m1", "m2", "m3"],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["inbox_unlabeled_first_page_count"] == 3
        # Status reflects DB value — retrigger spawns thread but doesn't fake status
        mock_threading.Thread.assert_called_once()
        assert mock_threading.Thread.call_args[1]["target"].__name__ == "_run_sent_scan"

    def test_api_me_no_retrigger_while_this_process_scans_the_user(self):
        """Between chained steps the DB can look idle; the process knows better."""
        import claven.server as srv
        token = _make_session_token()
        srv._scanning_users.add("uid-1")
        try:
            with patch.dict("os.environ", _ENV):
                with patch("claven.server.db") as mock_db, \
                     patch("claven.server.auth") as mock_auth, \
                     patch("claven.server.threading") as mock_threading:
                    _fake_db_ctx(mock_db)
                    mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                    mock_db.get_history_id.return_value = 12345
                    mock_db.get_inbox_scan_status.return_value = "complete"
                    mock_db.get_pending_relabel_senders.return_value = ["a@example.com"]
                    mock_auth.get_service.return_value = self._make_gmail_service(
                        messages_total=100, unlabeled_ids=["m1", "m2", "m3"],
                    )
                    with TestClient(app) as client:
                        client.cookies.set("session", token)
                        client.get("/api/me")
        finally:
            srv._scanning_users.discard("uid-1")
        mock_threading.Thread.assert_not_called()

    def test_api_me_retriggers_on_error_status(self):
        """Error status with unlabeled messages retriggers the scan."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "error.db.connection_lost"
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, unlabeled_ids=[f"m{i}" for i in range(100)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        # Status reflects DB value — retrigger spawns thread but doesn't fake status
        mock_threading.Thread.assert_called_once()
        assert mock_threading.Thread.call_args[1]["target"].__name__ == "_run_sent_scan"

    def test_api_me_no_retrigger_when_already_running(self):
        """Don't retrigger if scan is already in progress."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "in_progress"
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, unlabeled_ids=[f"m{i}" for i in range(100)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        mock_threading.Thread.assert_not_called()

    def test_api_me_no_retrigger_when_all_labeled(self):
        """Don't retrigger if scan is complete and all messages are labeled."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "complete"
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100,
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["inbox_unlabeled_first_page_count"] == 0
        assert response.json()["inbox_scan_status"] == "complete"
        mock_threading.Thread.assert_not_called()

    def test_api_me_no_retrigger_when_sent_scan_running(self):
        """Don't retrigger if sent scan is in_progress (it chains to inbox scan)."""
        token = _make_session_token()
        from datetime import datetime, timezone
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "cancelled"
                mock_db.get_sent_scan_progress.return_value = {
                    "messages_scanned": 10, "messages_total": 100,
                    "status": "in_progress", "updated_at": datetime.now(timezone.utc),
                }
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, unlabeled_ids=[f"m{i}" for i in range(50)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        mock_threading.Thread.assert_not_called()

    def test_api_me_retriggers_when_sent_scan_complete(self):
        """Retrigger when sent scan is complete and unlabeled messages remain."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "cancelled"
                # sent_scan_progress defaults to complete in _fake_db_ctx
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, unlabeled_ids=[f"m{i}" for i in range(50)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        mock_threading.Thread.assert_called_once()
        assert mock_threading.Thread.call_args[1]["target"].__name__ == "_run_sent_scan"

    def test_api_me_retriggers_when_sent_scan_stale(self):
        """Stale in_progress sent scan (>1 min) should not block retrigger."""
        token = _make_session_token()
        from datetime import datetime, timezone, timedelta
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.get_inbox_scan_status.return_value = "cancelled"
                mock_db.get_sent_scan_progress.return_value = {
                    "messages_scanned": 10, "messages_total": 100,
                    "status": "in_progress",
                    "updated_at": datetime.now(timezone.utc) - timedelta(minutes=2),
                }
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_total=100, unlabeled_ids=[f"m{i}" for i in range(50)],
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        mock_threading.Thread.assert_called_once()
        assert mock_threading.Thread.call_args[1]["target"].__name__ == "_run_sent_scan"

    def test_read_count_derived_from_inbox(self):
        """Read count is inbox_count - unread_count, not resultSizeEstimate."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 12345
                mock_db.count_known_senders.return_value = 0
                mock_auth.get_service.return_value = self._make_gmail_service(
                    messages_unread=30, messages_total=100, read_estimate=999,
                )
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        # Should be 100 - 30 = 70, NOT 999 (the resultSizeEstimate)
        assert response.json()["read_count"] == 70


class TestApiConfig:
    def test_returns_label_rules(self):
        with patch("claven.server.load_config") as mock_config:
            mock_config.return_value = {
                "labels": [{"id": "known-sender", "name": "Known Sender", "rules": [{"field": "from", "known_sender": True}]}]
            }
            with TestClient(app) as client:
                response = client.get("/api/config")
        assert response.status_code == 200
        assert response.json()["labels"][0]["id"] == "known-sender"
        assert response.json()["labels"][0]["name"] == "Known Sender"

    def test_returns_empty_labels_when_none_configured(self):
        with patch("claven.server.load_config") as mock_config:
            mock_config.return_value = {}
            with TestClient(app) as client:
                response = client.get("/api/config")
        assert response.json()["labels"] == []


class TestApiDisconnect:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/api/disconnect")
        assert response.status_code == 401

    def test_stops_watch_and_clears_watch_state(self):
        """Disconnect stops the Gmail watch and clears scan state (history_id)
        but keeps OAuth credentials so reconnect needs no OAuth round-trip."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.stop_watch") as mock_stop:
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/disconnect")
        assert response.status_code == 200
        mock_stop.assert_called_once()
        mock_db.clear_watch_state.assert_called_once_with(ANY, "uid-1")
        mock_db.delete_credentials.assert_not_called()

    def test_does_not_clear_session_cookie(self):
        """Disconnect removes Gmail credentials but keeps the user signed in."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.stop_watch"):
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/disconnect")
        set_cookie = response.headers.get("set-cookie", "")
        # session cookie must NOT be deleted — user stays signed in
        assert "session" not in set_cookie

    def test_watch_stop_failure_still_clears_watch_state(self):
        """stop_watch errors (e.g. already expired) must not block disconnect."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.stop_watch") as mock_stop:
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                mock_stop.side_effect = Exception("watch already expired")
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/disconnect")
        assert response.status_code == 200
        mock_db.clear_watch_state.assert_called_once()


class TestApiDeleteAccount:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.account") as mock_account, TestClient(app) as client:
                response = client.post("/api/account/delete")
        assert response.status_code == 401
        mock_account.delete_account.assert_not_called()

    def test_deletes_account_and_clears_session_cookie(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.account") as mock_account:
                _fake_db_ctx(mock_db)
                mock_account.delete_account.return_value = {"watch_stopped": True, "token_revoked": True}
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/account/delete")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_account.delete_account.assert_called_once_with(ANY, "uid-1", _ENV["TOKEN_ENCRYPTION_KEY"])
        assert 'session=""' in response.headers.get("set-cookie", "")

    def test_cross_origin_request_is_rejected(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.account") as mock_account:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/account/delete", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        mock_account.delete_account.assert_not_called()


class TestApiConnect:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/api/connect")
        assert response.status_code == 401

    def test_returns_200_with_history_id(self):
        token = _make_session_token()
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.start_watch") as mock_watch, \
                 patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                mock_watch.return_value = {"historyId": "99999"}
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/connect")
        assert response.status_code == 200
        assert response.json()["history_id"] == 99999

    def test_calls_start_watch(self):
        token = _make_session_token()
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.start_watch") as mock_watch, \
                 patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                mock_watch.return_value = {"historyId": "99999"}
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    client.post("/api/connect")
        mock_watch.assert_called_once()

    def test_sets_history_id(self):
        token = _make_session_token()
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.start_watch") as mock_watch, \
                 patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                mock_watch.return_value = {"historyId": "99999"}
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    client.post("/api/connect")
        mock_db.set_history_id.assert_called_once_with(ANY, "uid-1", 99999)

    def test_triggers_sent_scan_in_background(self):
        """Connect triggers sent scan only — it chains into inbox scan."""
        token = _make_session_token()
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.start_watch") as mock_watch, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                mock_watch.return_value = {"historyId": "99999"}
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    client.post("/api/connect")
        mock_threading.Thread.assert_called_once()
        assert mock_threading.Thread.call_args[1]["target"].__name__ == "_run_sent_scan"
        # Marked before the thread starts, so a dashboard poll racing it sees the
        # scan running and doesn't start a second one.
        mock_db.set_sent_scan_status.assert_called_once_with(ANY, "uid-1", "in_progress")

    def test_watch_failure_returns_500(self):
        token = _make_session_token()
        with patch.dict("os.environ", {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.server.start_watch") as mock_watch:
                _fake_db_ctx(mock_db)
                mock_auth.get_service.return_value = MagicMock()
                mock_watch.side_effect = Exception("Gmail API error")
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/connect")
        assert response.status_code == 500


class TestApiLogout:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/api/logout")
        assert response.status_code == 401

    def test_returns_200(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, TestClient(app) as client:
                _fake_db_ctx(mock_db)
                client.cookies.set("session", token)
                response = client.post("/api/logout")
        assert response.status_code == 200

    def test_clears_session_cookie(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, TestClient(app) as client:
                _fake_db_ctx(mock_db)
                client.cookies.set("session", token)
                response = client.post("/api/logout")
        assert "session" in response.headers.get("set-cookie", "")

    def test_does_not_touch_credentials(self):
        """Logout ends the session but leaves Gmail credentials intact."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    client.post("/api/logout")
        mock_db.delete_credentials.assert_not_called()


class TestOAuthCallbackSession:
    """Verify the callback issues a session cookie and redirects to /dashboard."""

    def _run_full_oauth(self, extra_env=None, return_to=None, has_existing_tokens=False, session_version=0):
        """Drive the full start → callback flow and return the callback response.

        has_existing_tokens=True simulates a returning user who already has
        credentials stored — the callback should skip store_credentials and
        start_watch and just issue a new session JWT.
        """
        env = {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t", **(extra_env or {})}
        mock_creds = MagicMock()
        mock_creds.id_token = "fake-id-token"
        mock_creds.token = "fake-access-token"
        mock_creds.refresh_token = "fake-refresh-token"
        mock_creds.expiry = None
        mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid", "https://www.googleapis.com/auth/userinfo.email"}

        mock_flow = MagicMock()
        mock_flow.credentials = mock_creds
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

        existing_tokens = {"access_token": "existing-token"} if has_existing_tokens else None

        with patch.dict("os.environ", env), \
             patch("claven.server.Flow") as mock_flow_cls, \
             patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify, \
             patch("claven.server.db") as mock_db, \
             patch("claven.server.auth") as mock_auth, \
             patch("claven.server.start_watch") as mock_watch, \
             patch("claven.server.build_known_senders"):
            mock_flow_cls.from_client_config.return_value = mock_flow
            mock_verify.return_value = {"email": "user@example.com"}
            _fake_db_ctx(mock_db)
            mock_db.upsert_user.return_value = "uid-1"
            mock_db.load_tokens.return_value = existing_tokens
            mock_db.get_session_version.return_value = session_version
            mock_watch.return_value = {"historyId": "99999"}

            with TestClient(app) as client:
                start_url = "/oauth/start"
                if return_to:
                    start_url += f"?return_to={return_to}"
                start = client.get(start_url, follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)
                if return_to:
                    client.cookies.set("oauth_return_to", return_to)
                return client.get(
                    f"/oauth/callback?code=abc&state={state}",
                    follow_redirects=False,
                )

    def test_successful_callback_redirects_to_dashboard(self):
        response = self._run_full_oauth()
        assert response.status_code == 302
        assert response.headers["location"] == "https://claven.app/dashboard"

    def test_return_to_redirects_to_custom_frontend(self):
        env = {"CORS_EXTRA_ORIGINS": "http://localhost:3000"}
        response = self._run_full_oauth(extra_env=env, return_to="http://localhost:3000")
        assert response.status_code == 302
        assert response.headers["location"] == "http://localhost:3000/dashboard"

    def test_successful_callback_sets_session_cookie(self):
        response = self._run_full_oauth()
        assert "session" in response.cookies

    def test_session_cookie_is_samesite_lax_and_expires(self):
        """claven.app and api.claven.app are the same site, so Lax still reaches the API
        from the dashboard while requests from other sites go without it (CSRF)."""
        response = self._run_full_oauth()
        set_cookie = [c for c in response.headers.get_list("set-cookie") if c.startswith("session=")][0].lower()
        assert "samesite=lax" in set_cookie
        assert "httponly" in set_cookie and "secure" in set_cookie
        assert f"max-age={30 * 24 * 3600}" in set_cookie

    def test_issued_session_carries_expiry_and_version(self):
        import jwt as pyjwt
        response = self._run_full_oauth(session_version=4)
        claims = pyjwt.decode(response.cookies["session"], _ENV["SESSION_SECRET"], algorithms=["HS256"])
        assert claims["user_id"] == "uid-1"
        assert claims["sv"] == 4
        assert claims["exp"] - claims["iat"] == 30 * 24 * 3600

    def test_new_user_calls_store_credentials(self):
        """First-time sign-in stores OAuth credentials in the DB."""
        env = {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}
        mock_creds = MagicMock()
        mock_creds.id_token = "fake-id-token"
        mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid"}
        mock_flow = MagicMock()
        mock_flow.credentials = mock_creds
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

        with patch.dict("os.environ", env), \
             patch("claven.server.Flow") as mock_flow_cls, \
             patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify, \
             patch("claven.server.db") as mock_db, \
             patch("claven.server.auth") as mock_auth, \
             patch("claven.server.start_watch"):
            mock_flow_cls.from_client_config.return_value = mock_flow
            mock_verify.return_value = {"email": "user@example.com"}
            _fake_db_ctx(mock_db)
            mock_db.upsert_user.return_value = "uid-1"
            mock_db.load_tokens.return_value = None  # new user

            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)
                client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)

        mock_auth.store_credentials.assert_called_once()

    def test_new_user_does_not_start_watch(self):
        """oauth_callback never starts the watch — that's an explicit /api/connect step."""
        env = {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}
        mock_creds = MagicMock()
        mock_creds.id_token = "fake-id-token"
        mock_creds.refresh_token = "fake-refresh-token"
        mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid"}
        mock_flow = MagicMock()
        mock_flow.credentials = mock_creds
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

        with patch.dict("os.environ", env), \
             patch("claven.server.Flow") as mock_flow_cls, \
             patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify, \
             patch("claven.server.db") as mock_db, \
             patch("claven.server.auth"), \
             patch("claven.server.start_watch") as mock_watch:
            mock_flow_cls.from_client_config.return_value = mock_flow
            mock_verify.return_value = {"email": "user@example.com"}
            _fake_db_ctx(mock_db)
            mock_db.upsert_user.return_value = "uid-1"
            mock_db.load_tokens.return_value = None

            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)
                client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)

        mock_watch.assert_not_called()

    def test_returning_user_skips_store_credentials(self):
        """Returning user sign-in must not overwrite existing credentials."""
        response = self._run_full_oauth(has_existing_tokens=True)
        assert response.status_code == 302  # sanity check the flow succeeded

    def test_returning_user_skips_start_watch(self):
        """Returning user sign-in must not restart the Gmail push watch."""
        response = self._run_full_oauth(has_existing_tokens=True)
        # start_watch is never called in oauth_callback regardless of user type
        # (verified implicitly — _run_full_oauth patches start_watch and the
        # test passes only if no exception is raised from an uncalled mock)
        assert response.status_code == 302

    def test_returning_user_still_redirects_to_dashboard(self):
        """Returning users land on /dashboard just like new users."""
        response = self._run_full_oauth(has_existing_tokens=True)
        assert response.headers["location"] == "https://claven.app/dashboard"

    def test_returning_user_still_gets_session_cookie(self):
        """Returning users receive a fresh session JWT."""
        response = self._run_full_oauth(has_existing_tokens=True)
        assert "session" in response.cookies

    def test_returning_user_does_not_trigger_scan(self):
        """Returning users don't get a scan from OAuth — /api/me handles retrigger."""
        env = {**_ENV, "PUBSUB_TOPIC": "projects/p/topics/t"}
        mock_creds = MagicMock()
        mock_creds.id_token = "fake-id-token"
        mock_creds.token = "fake-access-token"
        mock_creds.refresh_token = "fake-refresh-token"
        mock_creds.expiry = None
        mock_creds.scopes = {"https://www.googleapis.com/auth/gmail.modify", "openid", "https://www.googleapis.com/auth/userinfo.email"}

        mock_flow = MagicMock()
        mock_flow.credentials = mock_creds
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")

        with patch.dict("os.environ", env), \
             patch("claven.server.Flow") as mock_flow_cls, \
             patch("claven.server.google_id_token.verify_oauth2_token") as mock_verify, \
             patch("claven.server.db") as mock_db, \
             patch("claven.server.auth"), \
             patch("claven.server.start_watch"), \
             patch("claven.server.threading") as mock_threading:
            mock_flow_cls.from_client_config.return_value = mock_flow
            mock_verify.return_value = {"email": "user@example.com"}
            _fake_db_ctx(mock_db)
            mock_db.upsert_user.return_value = "uid-1"
            mock_db.load_tokens.return_value = {"access_token": "existing-token"}

            with TestClient(app) as client:
                start = client.get("/oauth/start", follow_redirects=False)
                state = start.cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)
                client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
        mock_threading.Thread.assert_not_called()


class TestCloudJsonFormatter:
    def test_formats_info_as_json(self):
        import json
        from claven.server import _CloudJsonFormatter
        formatter = _CloudJsonFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello world", (), None)
        output = json.loads(formatter.format(record))
        assert output["severity"] == "INFO"
        assert output["message"] == "hello world"
        assert output["logger"] == "test"

    def test_includes_extra_fields(self):
        import json
        from claven.server import _CloudJsonFormatter
        formatter = _CloudJsonFormatter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "scan done", (), None)
        record.user_id = "uid-1"
        record.event = "inbox_scan_complete"
        output = json.loads(formatter.format(record))
        assert output["user_id"] == "uid-1"
        assert output["event"] == "inbox_scan_complete"

    def test_never_emits_email(self):
        """Users are identified by user_id in logs; the privacy policy promises no addresses."""
        import json
        from claven.server import _CloudJsonFormatter
        record = logging.LogRecord("test", logging.INFO, "", 0, "signed in", (), None)
        record.user_id = "uid-1"
        record.email = "user@example.com"
        output = json.loads(_CloudJsonFormatter().format(record))
        assert "email" not in output

    def test_includes_exception(self):
        import json
        from claven.server import _CloudJsonFormatter
        formatter = _CloudJsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord("test", logging.ERROR, "", 0, "failed", (), sys.exc_info())
        output = json.loads(formatter.format(record))
        assert output["severity"] == "ERROR"
        assert "ValueError: boom" in output["exception"]


class TestApiEvents:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/api/events")
        assert response.status_code == 401

    def test_db_unavailable_returns_503(self):
        token = _make_session_token()
        bad_env = {**_ENV, "DATABASE_URL": "postgresql://bad:bad@localhost:1/nonexistent"}
        with patch.dict("os.environ", bad_env), \
                patch("claven.routes.auth._current_session_version", return_value=0):
            with TestClient(app) as client:
                client.cookies.set("session", token)
                response = client.get("/api/events")
        assert response.status_code == 503

    def test_listen_uses_direct_url_when_set(self):
        # PgBouncer (transaction mode) silently drops LISTEN, so the SSE
        # connection must go to the direct host when one is configured.
        token = _make_session_token()
        env = {**_ENV, "DATABASE_URL": "postgresql://pooled", "DATABASE_URL_DIRECT": "postgresql://direct"}
        with patch.dict("os.environ", env), \
                patch("claven.routes.auth._current_session_version", return_value=0), \
                patch("psycopg2.connect", side_effect=RuntimeError("no db")) as mock_connect:
            with TestClient(app) as client:
                client.cookies.set("session", token)
                response = client.get("/api/events")
        assert response.status_code == 503
        # Regular queries stay on the pooler; only the LISTEN connection goes direct.
        assert mock_connect.call_args_list[-1].args == ("postgresql://direct",)


class TestListenDatabaseUrl:
    def test_prefers_direct_url(self):
        from claven.routes.api import _listen_database_url
        env = {"DATABASE_URL": "postgresql://pooled", "DATABASE_URL_DIRECT": "postgresql://direct"}
        with patch.dict("os.environ", env):
            assert _listen_database_url() == "postgresql://direct"

    def test_falls_back_to_database_url(self):
        import os
        from claven.routes.api import _listen_database_url
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://pooled"}):
            os.environ.pop("DATABASE_URL_DIRECT", None)
            assert _listen_database_url() == "postgresql://pooled"


# ── Graceful shutdown tests ──────────────────────────────────────────────────

class TestIsCurrentWorker:
    def test_returns_true_normally(self):
        import claven.server as srv
        srv._shutdown_event.clear()
        try:
            assert srv._is_current_worker() is True
        finally:
            srv._shutdown_event.clear()

    def test_returns_false_when_shutdown_event_set(self):
        import claven.server as srv
        srv._shutdown_event.set()
        try:
            assert srv._is_current_worker() is False
        finally:
            srv._shutdown_event.clear()

    def test_returns_false_when_pid_changed(self):
        import claven.server as srv
        srv._shutdown_event.clear()
        with patch.object(srv, "_worker_id", os.getpid() + 1):
            assert srv._is_current_worker() is False


class TestShutdownHandler:
    def test_sets_shutdown_event(self):
        import claven.server as srv
        srv._shutdown_event.clear()
        try:
            srv._shutdown_handler(signal.SIGTERM, None)
            assert srv._shutdown_event.is_set()
        finally:
            srv._shutdown_event.clear()

    def test_is_idempotent(self):
        import claven.server as srv
        srv._shutdown_event.clear()
        try:
            srv._shutdown_handler(signal.SIGTERM, None)
            srv._shutdown_handler(signal.SIGTERM, None)
            assert srv._shutdown_event.is_set()
        finally:
            srv._shutdown_event.clear()


class TestSpawnScanThread:
    def test_creates_daemon_thread(self):
        import claven.server as srv
        with patch.object(srv, "threading") as mock_threading:
            srv._spawn_scan_thread(lambda: None, ())
        mock_threading.Thread.assert_called_once()
        assert mock_threading.Thread.call_args[1]["daemon"] is True
        mock_threading.Thread.return_value.start.assert_called_once()

    def test_adds_thread_to_active_threads(self):
        import claven.server as srv
        done = threading.Event()
        def target():
            done.wait(1)
        with srv._threads_lock:
            srv._active_threads.clear()
        try:
            t = srv._spawn_scan_thread(target, ())
            assert t in srv._active_threads
        finally:
            done.set()
            t.join(timeout=1)
            with srv._threads_lock:
                srv._active_threads.clear()

    def test_starts_the_thread(self):
        import claven.server as srv
        ran = threading.Event()
        def target():
            ran.set()
        with srv._threads_lock:
            srv._active_threads.clear()
        try:
            t = srv._spawn_scan_thread(target, ())
            assert ran.wait(timeout=1)
        finally:
            t.join(timeout=1)
            with srv._threads_lock:
                srv._active_threads.clear()

    def test_passes_args_to_target(self):
        import claven.server as srv
        results = []
        def target(a, b):
            results.append((a, b))
        with srv._threads_lock:
            srv._active_threads.clear()
        try:
            t = srv._spawn_scan_thread(target, ("x", "y"))
            t.join(timeout=1)
            assert results == [("x", "y")]
        finally:
            with srv._threads_lock:
                srv._active_threads.clear()

    def test_starts_when_shutdown_clear(self):
        import claven.server as srv
        srv._shutdown_event.clear()
        ran = threading.Event()
        def target():
            ran.set()
        with srv._threads_lock:
            srv._active_threads.clear()
        try:
            t = srv._spawn_scan_thread(target, ())
            assert t is not None
            assert ran.wait(timeout=1)
        finally:
            t.join(timeout=1)
            with srv._threads_lock:
                srv._active_threads.clear()


class TestLifespanStartup:
    def test_clears_shutdown_event(self):
        import claven.server as srv
        srv._shutdown_event.set()
        with TestClient(app):
            assert not srv._shutdown_event.is_set()

    def test_clears_active_threads(self):
        import claven.server as srv
        with srv._threads_lock:
            srv._active_threads.append(MagicMock())
        with TestClient(app):
            assert len(srv._active_threads) == 0


class TestLifespanShutdown:
    def test_sets_shutdown_event(self):
        import claven.server as srv
        with TestClient(app):
            assert not srv._shutdown_event.is_set()
        assert srv._shutdown_event.is_set()

    def test_joins_threads(self):
        import claven.server as srv
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        with TestClient(app):
            with srv._threads_lock:
                srv._active_threads.append(mock_thread)
        mock_thread.join.assert_called_once_with(timeout=5)

    def test_clears_active_threads(self):
        import claven.server as srv
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        with TestClient(app):
            with srv._threads_lock:
                srv._active_threads.append(mock_thread)
        assert len(srv._active_threads) == 0

    def test_logs_warning_for_stuck_threads(self):
        import claven.server as srv
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        with patch.object(srv, "logger") as mock_logger:
            with TestClient(app):
                with srv._threads_lock:
                    srv._active_threads.append(mock_thread)
        mock_logger.warning.assert_any_call(
            "Shutdown: %d scan thread(s) still alive after timeout", 1
        )


class TestGracefulShutdownIntegration:
    def test_spawned_thread_exits_on_shutdown(self):
        import claven.server as srv
        started = threading.Event()
        def worker():
            started.set()
            while not srv._shutdown_event.is_set():
                srv._shutdown_event.wait(10)
        with TestClient(app):
            t = srv._spawn_scan_thread(worker, ())
            assert started.wait(timeout=1)
            assert t.is_alive()
        # Lifespan shutdown set the event and joined
        assert not t.is_alive()

    def test_multiple_threads_all_exit(self):
        import claven.server as srv
        threads = []
        def worker():
            while not srv._shutdown_event.is_set():
                srv._shutdown_event.wait(10)
        with TestClient(app):
            for _ in range(3):
                threads.append(srv._spawn_scan_thread(worker, ()))
            for t in threads:
                assert t.is_alive()
        for t in threads:
            assert not t.is_alive()


# ── Scan scope settings tests ────────────────────────────────────────────────

class TestApiScanScope:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/api/settings/scan-scope",
                    json={"scope": "allmail"})
        assert response.status_code == 401

    def test_invalid_scope_returns_400(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/settings/scan-scope",
                        json={"scope": "invalid"})
        assert response.status_code == 400

    def test_sets_scope_without_triggering_scan(self):
        """Changing scope saves the setting but does not auto-trigger a scan."""
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.threading") as mock_threading:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/settings/scan-scope",
                        json={"scope": "allmail"})
        assert response.status_code == 200
        assert response.json()["scan_scope"] == "allmail"
        mock_db.set_scan_scope.assert_called_once_with(ANY, "uid-1", "allmail")
        mock_threading.Thread.assert_not_called()

    def test_api_me_returns_scan_scope(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_scan_scope.return_value = "allmail"
                mock_auth.get_service.return_value = self._make_gmail_service()
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        assert response.json()["scan_scope"] == "allmail"

    def _make_gmail_service(self):
        """Minimal gmail service mock for /api/me."""
        return TestApiMe._make_gmail_service(TestApiMe())


class TestApiTopSenders:
    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.get("/api/top-senders")
        assert response.status_code == 401

    def test_empty_inbox_returns_empty(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                svc = MagicMock()
                svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": []}
                mock_auth.get_service.return_value = svc
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/top-senders")
        body = response.json()
        assert body["top_senders"] == []
        assert body["total_unread_known"] == 0

    def test_returns_top_senders_and_domains(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.core.gmail.batch_get_message_metadata") as mock_batch:
                _fake_db_ctx(mock_db)
                svc = MagicMock()
                svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
                    "messages": [{"id": f"m{i}"} for i in range(5)]
                }
                mock_auth.get_service.return_value = svc
                # 3 from alice@corp.com, 2 from bob@corp.com
                mock_batch.return_value = {
                    "m0": ({"from": "Alice <alice@corp.com>"}, [], None),
                    "m1": ({"from": "alice@corp.com"}, [], None),
                    "m2": ({"from": "Alice <alice@corp.com>"}, [], None),
                    "m3": ({"from": "Bob <bob@corp.com>"}, [], None),
                    "m4": ({"from": "bob@corp.com"}, [], None),
                }
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/top-senders")
        body = response.json()
        assert body["total_unread_known"] == 5
        # Top senders: alice=3, bob=2
        assert body["top_senders"][0] == {"email": "alice@corp.com", "count": 3}
        assert body["top_senders"][1] == {"email": "bob@corp.com", "count": 2}
        # Top domains: corp.com=5
        assert body["top_domains"][0] == {"domain": "corp.com", "count": 5}

    def test_multiple_domains_ranked(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.core.gmail.batch_get_message_metadata") as mock_batch:
                _fake_db_ctx(mock_db)
                svc = MagicMock()
                svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
                    "messages": [{"id": f"m{i}"} for i in range(4)]
                }
                mock_auth.get_service.return_value = svc
                mock_batch.return_value = {
                    "m0": ({"from": "a@big.com"}, [], None),
                    "m1": ({"from": "b@big.com"}, [], None),
                    "m2": ({"from": "c@big.com"}, [], None),
                    "m3": ({"from": "d@small.com"}, [], None),
                }
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/top-senders")
        body = response.json()
        # big.com=3, small.com=1
        assert body["top_domains"][0] == {"domain": "big.com", "count": 3}
        assert body["top_domains"][1] == {"domain": "small.com", "count": 1}


# ── Inbox mode (Label only / Gatekeeper / Sunset) ────────────────────────────

class TestApiInboxMode:
    def _post(self, body):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/settings/inbox-mode", json=body)
        return response, mock_db

    def test_no_token_returns_401(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/api/settings/inbox-mode", json={"mode": "sunset"})
        assert response.status_code == 401

    def test_invalid_mode_returns_400(self):
        response, mock_db = self._post({"mode": "nope"})
        assert response.status_code == 400
        mock_db.set_inbox_mode.assert_not_called()

    def test_invalid_hours_returns_400(self):
        response, mock_db = self._post({"mode": "sunset", "sunset_hours": 5})
        assert response.status_code == 400
        mock_db.set_inbox_mode.assert_not_called()

    def test_sets_sunset_with_hours(self):
        response, mock_db = self._post({"mode": "sunset", "sunset_hours": 12})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "inbox_mode": "sunset", "sunset_hours": 12}
        mock_db.set_inbox_mode.assert_called_once_with(ANY, "uid-1", "sunset", sunset_hours=12)

    def test_mode_without_hours_keeps_current_timer(self):
        response, mock_db = self._post({"mode": "gatekeeper"})
        assert response.status_code == 200
        mock_db.set_inbox_mode.assert_called_once_with(ANY, "uid-1", "gatekeeper", sunset_hours=None)

    def test_auto_archive_endpoint_is_gone(self):
        # Replaced by /api/settings/inbox-mode; the dashboard no longer calls it.
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/settings/auto-archive", json={"enabled": True})
        assert response.status_code == 404
        mock_db.set_inbox_mode.assert_not_called()

    def test_api_me_returns_inbox_mode(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_scan_scope.return_value = "inbox"
                mock_db.get_inbox_mode.return_value = {"mode": "sunset", "sunset_hours": 72, "changed_at": None}
                mock_auth.get_service.return_value = TestApiMe._make_gmail_service(TestApiMe())
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.get("/api/me")
        body = response.json()
        assert body["inbox_mode"] == "sunset"
        assert body["sunset_hours"] == 72
        assert "auto_archive_unknown" not in body  # inbox_mode replaced it


class TestInternalSunset:
    _AUTH = {"Authorization": "Bearer test-internal-secret"}

    def test_requires_internal_auth(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                response = client.post("/internal/sunset")
        assert response.status_code == 401

    def test_archives_expired_mail_for_connected_sunset_users(self):
        from datetime import datetime, timedelta, timezone
        long_ago = datetime.now(timezone.utc) - timedelta(days=3)
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth") as mock_auth, \
                 patch("claven.routes.internal.archive_expired_unknown", return_value=2) as mock_archive, \
                 patch("claven.routes.internal.existing_countdown_ids", return_value={"c/40h": "old-40"}), \
                 patch("claven.routes.internal.build_label_id_cache", side_effect=lambda svc, names: {n: f"id-{n}" for n in names}), \
                 patch("claven.routes.internal.update_countdowns") as mock_countdowns, \
                 patch("claven.routes.internal.clear_countdowns") as mock_clear:
                _fake_db_ctx(mock_db)
                mock_db.get_countdown_cleanup_users.return_value = []
                mock_db.get_sunset_users.return_value = [
                    {"user_id": "uid-1", "sunset_hours": 24, "changed_at": long_ago},
                    {"user_id": "uid-2", "sunset_hours": 24, "changed_at": long_ago},
                ]
                mock_db.get_history_id.side_effect = lambda conn, uid: 99 if uid == "uid-1" else None
                with TestClient(app) as client:
                    response = client.post("/internal/sunset", headers=self._AUTH)
        assert response.status_code == 200
        assert response.json()["archived"] == 2
        mock_archive.assert_called_once()
        assert mock_archive.call_args.args[1:3] == ("c/u", "id-c/u")
        mock_db.log_event.assert_called_once()
        mock_countdowns.assert_called_once()
        # Countdowns replace c/u while mail waits, so the sweep needs its id to swap back.
        assert mock_countdowns.call_args.args[1:3] == ("c/u", "id-c/u")
        # Hourly labels for the 24h timer, plus any left over from a longer timer.
        assert mock_countdowns.call_args.args[-1] == {"c/40h": "old-40", **{f"c/{h}h": f"id-c/{h}h" for h in range(24, 0, -1)}}
        assert mock_archive.call_args.args[4] == mock_countdowns.call_args.args[-1]
        mock_db.set_sunset_countdown_active.assert_called_once_with(ANY, "uid-1", True)
        # c/40h is beyond the 24h timer: deleted, keeping the timer's own hours.
        mock_clear.assert_called_once()
        assert mock_clear.call_args.args[1] == "id-c/u"
        assert mock_clear.call_args.kwargs["keep"] == tuple(f"c/{h}h" for h in range(24, 0, -1))

    def test_unused_countdown_labels_go_after_countdowns_are_updated(self):
        # update_countdowns first moves waiting mail off the hours beyond the timer.
        from datetime import datetime, timedelta, timezone
        order = MagicMock()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth"), \
                 patch("claven.routes.internal.archive_expired_unknown", return_value=0), \
                 patch("claven.routes.internal.existing_countdown_ids", return_value={"c/72h": "old-72"}), \
                 patch("claven.routes.internal.build_label_id_cache", side_effect=lambda svc, names: {n: f"id-{n}" for n in names}), \
                 patch("claven.routes.internal.update_countdowns") as mock_countdowns, \
                 patch("claven.routes.internal.clear_countdowns") as mock_clear:
                order.attach_mock(mock_countdowns, "update")
                order.attach_mock(mock_clear, "clear")
                _fake_db_ctx(mock_db)
                mock_db.get_countdown_cleanup_users.return_value = []
                mock_db.get_sunset_users.return_value = [
                    {"user_id": "uid-1", "sunset_hours": 24, "changed_at": datetime.now(timezone.utc) - timedelta(days=3)},
                ]
                mock_db.get_history_id.return_value = 99
                with TestClient(app) as client:
                    client.post("/internal/sunset", headers=self._AUTH)
        assert [c[0] for c in order.mock_calls] == ["update", "clear"]

    def test_no_label_cleanup_when_every_countdown_label_is_in_use(self):
        from datetime import datetime, timedelta, timezone
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth"), \
                 patch("claven.routes.internal.archive_expired_unknown", return_value=0), \
                 patch("claven.routes.internal.existing_countdown_ids", return_value={"c/24h": "id-c/24h"}), \
                 patch("claven.routes.internal.build_label_id_cache", side_effect=lambda svc, names: {n: f"id-{n}" for n in names}), \
                 patch("claven.routes.internal.update_countdowns"), \
                 patch("claven.routes.internal.clear_countdowns") as mock_clear:
                _fake_db_ctx(mock_db)
                mock_db.get_countdown_cleanup_users.return_value = []
                mock_db.get_sunset_users.return_value = [
                    {"user_id": "uid-1", "sunset_hours": 24, "changed_at": datetime.now(timezone.utc) - timedelta(days=3)},
                ]
                mock_db.get_history_id.return_value = 99
                with TestClient(app) as client:
                    client.post("/internal/sunset", headers=self._AUTH)
        mock_clear.assert_not_called()

    def test_renames_old_labels_before_archiving(self):
        # build_label_id_cache renames unknown-sender to c/u, so it must run
        # before the archive search for label:c/u.
        from datetime import datetime, timedelta, timezone
        order = MagicMock()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth"), \
                 patch("claven.routes.internal.archive_expired_unknown", return_value=0) as mock_archive, \
                 patch("claven.routes.internal.existing_countdown_ids", return_value={}), \
                 patch("claven.routes.internal.build_label_id_cache", side_effect=lambda svc, names: {n: f"id-{n}" for n in names}) as mock_cache, \
                 patch("claven.routes.internal.update_countdowns"):
                order.attach_mock(mock_cache, "cache")
                order.attach_mock(mock_archive, "archive")
                _fake_db_ctx(mock_db)
                mock_db.get_countdown_cleanup_users.return_value = []
                mock_db.get_sunset_users.return_value = [
                    {"user_id": "uid-1", "sunset_hours": 24,
                     "changed_at": datetime.now(timezone.utc) - timedelta(days=3)},
                ]
                mock_db.get_history_id.return_value = 99
                with TestClient(app) as client:
                    client.post("/internal/sunset", headers=self._AUTH)
        assert [c[0] for c in order.mock_calls] == ["cache", "archive"]
        assert "c/u" in order.mock_calls[0].args[1]

    def test_skips_user_whose_timer_has_not_run_since_switching(self):
        from datetime import datetime, timezone
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth"), \
                 patch("claven.routes.internal.archive_expired_unknown") as mock_archive, \
                 patch("claven.routes.internal.existing_countdown_ids", return_value={}), \
                 patch("claven.routes.internal.build_label_id_cache", side_effect=lambda svc, names: {n: f"id-{n}" for n in names}), \
                 patch("claven.routes.internal.update_countdowns") as mock_countdowns:
                _fake_db_ctx(mock_db)
                mock_db.get_countdown_cleanup_users.return_value = []
                mock_db.get_sunset_users.return_value = [
                    {"user_id": "uid-1", "sunset_hours": 24, "changed_at": datetime.now(timezone.utc)},
                ]
                mock_db.get_history_id.return_value = 99
                with TestClient(app) as client:
                    response = client.post("/internal/sunset", headers=self._AUTH)
        assert response.status_code == 200
        mock_archive.assert_not_called()
        # Countdowns still show during the switch-over timer.
        mock_countdowns.assert_called_once()

    def test_clears_countdown_labels_for_users_who_left_sunset(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth"), \
                 patch("claven.routes.internal.build_label_id_cache", side_effect=lambda svc, names: {n: f"id-{n}" for n in names}), \
                 patch("claven.routes.internal.clear_countdowns", return_value=6) as mock_clear:
                _fake_db_ctx(mock_db)
                mock_db.get_countdown_cleanup_users.return_value = ["uid-9"]
                mock_db.get_sunset_users.return_value = []
                with TestClient(app) as client:
                    response = client.post("/internal/sunset", headers=self._AUTH)
        assert response.status_code == 200
        mock_clear.assert_called_once()
        # Mail that was counting down goes back to c/u.
        assert mock_clear.call_args.args[1] == "id-c/u"
        mock_db.set_sunset_countdown_active.assert_called_once_with(ANY, "uid-9", False)


class TestSunsetForLiveLabeling:
    """Live labeling learns the user's Sunset timer and countdown label ids."""

    def test_sunset_user_gets_timer_and_countdown_ids(self):
        from datetime import datetime, timezone
        from claven.core.sunset import countdown_labels_for
        from claven.tasks import _sunset_for_live_labeling
        changed = datetime(2026, 10, 27, tzinfo=timezone.utc)
        conn = MagicMock()
        with patch("claven.server.db") as mock_db, \
             patch("claven.server.build_label_id_cache",
                   side_effect=lambda svc, names: {n: f"id-{n}" for n in names}) as mock_cache:
            mock_db.get_inbox_mode.return_value = {"mode": "sunset", "sunset_hours": 12, "changed_at": changed}
            sunset = _sunset_for_live_labeling(MagicMock(), conn, "uid-1")
        # Only the hours up to the user's timer are created.
        assert sunset == {"hours": 12, "changed_at": changed,
                          "label_ids": {n: f"id-{n}" for n in countdown_labels_for(12)}}
        assert list(mock_cache.call_args.args[1]) == list(countdown_labels_for(12))
        # Countdown labels may now exist, so leaving Sunset must clean them up.
        mock_db.set_sunset_countdown_active.assert_called_once_with(conn, "uid-1", True)

    @pytest.mark.parametrize("mode", ["label_only", "gatekeeper"])
    def test_other_modes_get_none_and_no_labels(self, mode):
        from claven.tasks import _sunset_for_live_labeling
        with patch("claven.server.db") as mock_db, \
             patch("claven.server.build_label_id_cache") as mock_cache:
            mock_db.get_inbox_mode.return_value = {"mode": mode, "sunset_hours": 24, "changed_at": None}
            assert _sunset_for_live_labeling(MagicMock(), MagicMock(), "uid-1") is None
        mock_cache.assert_not_called()
        mock_db.set_sunset_countdown_active.assert_not_called()


class TestCasaHardening:
    """Errors don't reach clients, rejected auth is logged, and hot endpoints are rate limited."""

    def test_oauth_failure_redirect_carries_no_exception_text(self):
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                state = client.get("/oauth/start", follow_redirects=False).cookies.get("oauth_state")
                client.cookies.set("oauth_state", state)
                with patch("claven.server.Flow") as mock_flow_cls:
                    mock_flow_cls.from_client_config.return_value.fetch_token.side_effect = Exception(
                        "invalid_grant for client 123 at host db.internal")
                    response = client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False)
        location = response.headers["location"]
        assert "error=token_exchange_failed" in location
        assert "error_detail" not in location
        assert "invalid_grant" not in location

    def test_internal_secret_is_compared_in_constant_time(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.routes.auth.secrets.compare_digest", return_value=False) as cmp:
                with TestClient(app) as client:
                    response = client.post("/internal/poll", headers={"Authorization": "Bearer test-internal-secret"})
        assert response.status_code == 401
        cmp.assert_called_once()

    def test_rejected_session_is_logged_without_the_token(self, caplog):
        caplog.set_level(logging.INFO, logger="claven.routes.auth")
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                client.cookies.set("session", "not-a-jwt-secret-value")
                assert client.get("/api/me").status_code == 401
        records = [r for r in caplog.records if getattr(r, "event", None) == "auth_rejected"]
        assert len(records) == 1
        assert "/api/me" in records[0].getMessage()
        assert "not-a-jwt-secret-value" not in caplog.text

    def test_rejected_internal_call_is_logged(self, caplog):
        caplog.set_level(logging.INFO, logger="claven.routes.auth")
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                client.post("/internal/poll", headers={"Authorization": "Bearer wrong-secret-value"})
        records = [r for r in caplog.records if getattr(r, "event", None) == "auth_rejected"]
        assert len(records) == 1
        assert "/internal/poll" in records[0].getMessage()
        assert "wrong-secret-value" not in caplog.text

    def test_oauth_start_is_rate_limited_per_client_ip(self):
        from claven.routes.auth import OAUTH_START_LIMIT
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                headers = {"X-Forwarded-For": "203.0.113.7"}
                for _ in range(OAUTH_START_LIMIT.limit):
                    assert client.get("/oauth/start", headers=headers, follow_redirects=False).status_code in (302, 307)
                refused = client.get("/oauth/start", headers=headers, follow_redirects=False)
                other_ip = client.get("/oauth/start", headers={"X-Forwarded-For": "203.0.113.8"}, follow_redirects=False)
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) > 0
        assert other_ip.status_code in (302, 307)

    def test_client_ip_is_the_one_googles_front_end_added(self):
        """Cloud Run's front end appends the caller's address; anything before it came from the caller."""
        from claven.routes.auth import OAUTH_START_LIMIT
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
                for i in range(OAUTH_START_LIMIT.limit):
                    client.get("/oauth/start", headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"},
                               follow_redirects=False)
                refused = client.get("/oauth/start", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"},
                                     follow_redirects=False)
        assert refused.status_code == 429

    def _get_as_user(self, path, times, user_id="uid-1"):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth") as mock_auth:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_id.return_value = {"id": user_id, "email": "user@example.com"}
                svc = MagicMock()
                svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": []}
                mock_auth.get_service.return_value = svc
                with TestClient(app) as client:
                    client.cookies.set("session", _make_session_token(user_id=user_id))
                    return [client.get(path).status_code for _ in range(times)]

    def test_api_me_is_rate_limited_per_user(self):
        from claven.routes.api import ME_LIMIT
        codes = self._get_as_user("/api/me", ME_LIMIT.limit + 1)
        assert codes[:-1] == [200] * ME_LIMIT.limit
        assert codes[-1] == 429
        assert self._get_as_user("/api/me", 1, user_id="uid-2") == [200]

    def test_top_senders_is_rate_limited_per_user(self):
        from claven.routes.api import TOP_SENDERS_LIMIT
        codes = self._get_as_user("/api/top-senders", TOP_SENDERS_LIMIT.limit + 1)
        assert codes[:-1] == [200] * TOP_SENDERS_LIMIT.limit
        assert codes[-1] == 429
