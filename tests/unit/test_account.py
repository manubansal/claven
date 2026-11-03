"""Unit tests for claven/core/account.py."""

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from claven.core.account import REVOKE_URL, delete_account, revoke_token
from claven.core.auth import GmailAccessRevoked

_KEY = "aa" * 32
_TOKENS = {"access_token_enc": b"a-enc", "refresh_token_enc": b"r-enc", "revoked_at": None}


class TestRevokeToken:
    def test_posts_token_to_google(self):
        with patch("claven.core.account.requests.post") as post:
            post.return_value.status_code = 200
            assert revoke_token("refresh-1") is True
        post.assert_called_once_with(REVOKE_URL, data={"token": "refresh-1"}, timeout=10)

    def test_rejected_returns_false(self):
        """Google answers 400 for a token it no longer knows (already revoked or expired)."""
        with patch("claven.core.account.requests.post") as post:
            post.return_value.status_code = 400
            assert revoke_token("refresh-1") is False

    def test_network_error_returns_false(self):
        with patch("claven.core.account.requests.post", side_effect=requests.ConnectionError("down")):
            assert revoke_token("refresh-1") is False


@pytest.fixture
def deps():
    """Mocks for everything delete_account talks to, attached to one parent to check call order."""
    parent = MagicMock()
    with patch("claven.core.account.db", parent.db), \
         patch("claven.core.account.auth", parent.auth), \
         patch("claven.core.account.stop_watch", parent.stop_watch), \
         patch("claven.core.account.revoke_token", parent.revoke_token):
        parent.db.load_tokens.return_value = dict(_TOKENS)
        parent.auth.decrypt_token.return_value = "refresh-1"
        parent.revoke_token.return_value = True
        yield parent


class TestDeleteAccount:
    def test_stops_jobs_watch_and_revokes_before_deleting(self, deps):
        conn = deps.conn
        result = delete_account(conn, "uid-1", _KEY)

        assert result == {"watch_stopped": True, "token_revoked": True}
        deps.auth.decrypt_token.assert_called_once_with(b"r-enc", _KEY)
        names = [c[0] for c in deps.mock_calls]
        assert names.index("db.set_cancel_state") < names.index("conn.commit") < names.index("stop_watch")
        assert names.index("stop_watch") < names.index("revoke_token") < names.index("db.delete_user")
        deps.db.set_cancel_state.assert_called_once_with(conn, "uid-1", "cancel_job")
        deps.revoke_token.assert_called_once_with("refresh-1")
        deps.db.delete_user.assert_called_once_with(conn, "uid-1")

    def test_watch_stop_failure_still_revokes_and_deletes(self, deps):
        deps.stop_watch.side_effect = Exception("watch already expired")
        result = delete_account(deps.conn, "uid-1", _KEY)
        assert result == {"watch_stopped": False, "token_revoked": True}
        deps.revoke_token.assert_called_once()
        deps.db.delete_user.assert_called_once()

    def test_get_service_failure_still_revokes_and_deletes(self, deps):
        deps.auth.get_service.side_effect = GmailAccessRevoked("uid-1", newly_revoked=True)
        result = delete_account(deps.conn, "uid-1", _KEY)
        assert result == {"watch_stopped": False, "token_revoked": True}
        deps.db.delete_user.assert_called_once()

    def test_revoke_failure_still_deletes(self, deps):
        deps.revoke_token.return_value = False
        result = delete_account(deps.conn, "uid-1", _KEY)
        assert result == {"watch_stopped": True, "token_revoked": False}
        deps.db.delete_user.assert_called_once()

    def test_undecryptable_token_still_deletes(self, deps):
        deps.auth.decrypt_token.side_effect = Exception("bad key")
        result = delete_account(deps.conn, "uid-1", _KEY)
        assert result["token_revoked"] is False
        deps.revoke_token.assert_not_called()
        deps.db.delete_user.assert_called_once()

    def test_no_tokens_just_deletes(self, deps):
        deps.db.load_tokens.return_value = None
        result = delete_account(deps.conn, "uid-1", _KEY)
        assert result == {"watch_stopped": False, "token_revoked": False}
        deps.auth.get_service.assert_not_called()
        deps.revoke_token.assert_not_called()
        deps.db.delete_user.assert_called_once()

    def test_already_revoked_access_skips_google(self, deps):
        """Google has already rejected the token, so there's no watch to stop or grant to revoke."""
        deps.db.load_tokens.return_value = {**_TOKENS, "revoked_at": "2026-11-01"}
        result = delete_account(deps.conn, "uid-1", _KEY)
        assert result == {"watch_stopped": False, "token_revoked": False}
        deps.auth.get_service.assert_not_called()
        deps.revoke_token.assert_not_called()
        deps.db.delete_user.assert_called_once()
