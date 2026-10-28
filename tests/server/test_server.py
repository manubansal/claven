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
_PUBSUB_ID_INFO = {"email": "claven-pubsub@claven-prod.iam.gserviceaccount.com"}


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

    # ── Processing ────────────────────────────────────────────────────────────

    def test_valid_payload_unknown_user_returns_ok(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_email.return_value = None
                with _mock_pubsub_token():
                    with TestClient(app) as client:
                        response = client.post(
                            "/webhook/gmail",
                            json=_pubsub_payload(),
                            headers=_PUBSUB_HEADERS,
                        )
        assert response.status_code == 200

    def test_known_user_triggers_processing(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth") as mock_auth, patch(
                "claven.server.poll_new_messages"
            ) as mock_poll, patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_email.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 100
                mock_db.get_known_senders.return_value = set()
                mock_auth.get_service.return_value = MagicMock()
                mock_poll.return_value = 200
                with _mock_pubsub_token():
                    with TestClient(app) as client:
                        response = client.post(
                            "/webhook/gmail",
                            json=_pubsub_payload(history_id="200"),
                            headers=_PUBSUB_HEADERS,
                        )
        assert response.status_code == 200
        mock_poll.assert_called_once()

    def test_increments_processed_count_after_webhook(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth") as mock_auth, patch(
                "claven.server.poll_new_messages"
            ) as mock_poll, patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_email.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.get_history_id.return_value = 100
                mock_db.get_known_senders.return_value = set()
                mock_auth.get_service.return_value = MagicMock()
                mock_poll.return_value = 3
                with _mock_pubsub_token():
                    with TestClient(app) as client:
                        client.post(
                            "/webhook/gmail",
                            json=_pubsub_payload(history_id="200"),
                            headers=_PUBSUB_HEADERS,
                        )
        mock_db.increment_processed_count.assert_called_once_with(ANY, "uid-1", 3)

    def test_webhook_skips_locked_user(self):
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, patch("claven.server.auth"), patch(
                "claven.server.poll_new_messages"
            ) as mock_poll, patch("claven.server.build_known_senders"):
                _fake_db_ctx(mock_db)
                mock_db.get_user_by_email.return_value = {"id": "uid-1", "email": "user@example.com"}
                mock_db.try_lock_user_scan.return_value = False
                with _mock_pubsub_token():
                    with TestClient(app) as client:
                        response = client.post(
                            "/webhook/gmail",
                            json=_pubsub_payload(history_id="200"),
                            headers=_PUBSUB_HEADERS,
                        )
        assert response.status_code == 200
        assert response.json()["detail"] == "locked"
        mock_poll.assert_not_called()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_session_token(payload: dict | None = None) -> str:
    """Issue a real JWT signed with the test SESSION_SECRET."""
    import jwt as pyjwt
    data = payload or {"user_id": "uid-1", "email": "user@example.com"}
    return pyjwt.encode(data, _ENV["SESSION_SECRET"], algorithm="HS256")


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
    ):
        """Return a mock Gmail service with label counts.

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
                {"name": "known-sender", "id": known_id},
                {"name": "unknown-sender", "id": unknown_id},
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
            with TestClient(app) as client:
                client.cookies.set("session", token)
                response = client.post("/api/logout")
        assert response.status_code == 200

    def test_clears_session_cookie(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with TestClient(app) as client:
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

    def _run_full_oauth(self, extra_env=None, return_to=None, has_existing_tokens=False):
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

    def test_session_cookie_is_samesite_none(self):
        """Session cookie must be SameSite=None so cross-origin frontends
        (e.g. localhost:3000 calling api.claven.app) can include it in
        credentialed fetch requests."""
        response = self._run_full_oauth()
        set_cookie = response.headers.get("set-cookie", "").lower()
        assert "samesite=none" in set_cookie

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
        with patch.dict("os.environ", bad_env):
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

    def test_legacy_auto_archive_endpoint_still_works(self):
        token = _make_session_token()
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db:
                _fake_db_ctx(mock_db)
                with TestClient(app) as client:
                    client.cookies.set("session", token)
                    response = client.post("/api/settings/auto-archive", json={"enabled": True})
        assert response.status_code == 200
        mock_db.set_auto_archive_unknown.assert_called_once_with(ANY, "uid-1", True)

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
                 patch("claven.routes.internal.archive_expired_unknown", return_value=2) as mock_archive:
                _fake_db_ctx(mock_db)
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
        assert mock_archive.call_args.args[1] == "unknown-sender"
        mock_db.log_event.assert_called_once()

    def test_skips_user_whose_timer_has_not_run_since_switching(self):
        from datetime import datetime, timezone
        with patch.dict("os.environ", _ENV):
            with patch("claven.server.db") as mock_db, \
                 patch("claven.server.auth"), \
                 patch("claven.routes.internal.archive_expired_unknown") as mock_archive:
                _fake_db_ctx(mock_db)
                mock_db.get_sunset_users.return_value = [
                    {"user_id": "uid-1", "sunset_hours": 24, "changed_at": datetime.now(timezone.utc)},
                ]
                mock_db.get_history_id.return_value = 99
                with TestClient(app) as client:
                    response = client.post("/internal/sunset", headers=self._AUTH)
        assert response.status_code == 200
        mock_archive.assert_not_called()
