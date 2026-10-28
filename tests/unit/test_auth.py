"""Unit tests for claven/core/auth.py — pure logic, no DB or network I/O."""

import pytest
from cryptography.fernet import InvalidToken
from unittest.mock import MagicMock, patch

from claven.core.auth import decrypt_token, encrypt_token, get_service, load_credentials, store_credentials

# 32 bytes of 0xaa — valid Fernet key material
VALID_KEY_HEX = "aa" * 32


class TestEncryptDecrypt:
    def test_round_trip(self):
        ciphertext = encrypt_token("my-secret-token", VALID_KEY_HEX)
        assert isinstance(ciphertext, bytes)
        assert decrypt_token(ciphertext, VALID_KEY_HEX) == "my-secret-token"

    def test_wrong_key_raises(self):
        other_key = "bb" * 32
        ciphertext = encrypt_token("my-token", VALID_KEY_HEX)
        with pytest.raises(InvalidToken):
            decrypt_token(ciphertext, other_key)

    def test_empty_string_round_trip(self):
        ciphertext = encrypt_token("", VALID_KEY_HEX)
        assert decrypt_token(ciphertext, VALID_KEY_HEX) == ""

    def test_ciphertext_randomised(self):
        # Fernet uses a random IV so two encryptions of the same plaintext differ
        ct1 = encrypt_token("same", VALID_KEY_HEX)
        ct2 = encrypt_token("same", VALID_KEY_HEX)
        assert ct1 != ct2


class TestStoreCredentials:
    def test_stores_encrypted_tokens(self):
        creds = MagicMock()
        creds.token = "access-token-value"
        creds.refresh_token = "refresh-token-value"
        creds.expiry = None
        creds.scopes = ["https://www.googleapis.com/auth/gmail.modify"]

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db:
            store_credentials(conn, "user-uuid", creds, VALID_KEY_HEX)

        mock_db.store_tokens.assert_called_once()
        args = mock_db.store_tokens.call_args[0]
        # args: (conn, user_id, access_enc, refresh_enc, expiry, scopes)
        assert decrypt_token(args[2], VALID_KEY_HEX) == "access-token-value"
        assert decrypt_token(args[3], VALID_KEY_HEX) == "refresh-token-value"

    def test_passes_expiry_and_scopes(self):
        from datetime import datetime, timezone

        expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
        creds = MagicMock()
        creds.token = "tok"
        creds.refresh_token = "ref"
        creds.expiry = expiry
        creds.scopes = ["scope-a", "scope-b"]

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db:
            store_credentials(conn, "uid", creds, VALID_KEY_HEX)

        args = mock_db.store_tokens.call_args[0]
        assert args[4] == expiry
        assert args[5] == ["scope-a", "scope-b"]


class TestLoadCredentials:
    def test_returns_none_when_no_tokens(self):
        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db:
            mock_db.load_tokens.return_value = None
            result = load_credentials(conn, "user-uuid", VALID_KEY_HEX)
        assert result is None

    def test_returns_credentials_with_decrypted_tokens(self):
        access_enc = encrypt_token("access-token", VALID_KEY_HEX)
        refresh_enc = encrypt_token("refresh-token", VALID_KEY_HEX)

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, patch.dict(
            "os.environ",
            {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
        ):
            mock_db.load_tokens.return_value = {
                "access_token_enc": access_enc,
                "refresh_token_enc": refresh_enc,
                "token_expiry": None,
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            }
            result = load_credentials(conn, "user-uuid", VALID_KEY_HEX)

        assert result is not None
        assert result.token == "access-token"
        assert result.refresh_token == "refresh-token"

    def test_credentials_include_scopes(self):
        access_enc = encrypt_token("tok", VALID_KEY_HEX)
        refresh_enc = encrypt_token("ref", VALID_KEY_HEX)
        scopes = ["https://www.googleapis.com/auth/gmail.modify"]

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, patch.dict(
            "os.environ",
            {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
        ):
            mock_db.load_tokens.return_value = {
                "access_token_enc": access_enc,
                "refresh_token_enc": refresh_enc,
                "token_expiry": None,
                "scopes": scopes,
            }
            result = load_credentials(conn, "user-uuid", VALID_KEY_HEX)

        assert list(result.scopes) == scopes

    def test_naive_expiry_passes_through_unchanged(self):
        """Naive expiry (already UTC) is left as-is — google-auth expects naive UTC."""
        from datetime import datetime

        access_enc = encrypt_token("tok", VALID_KEY_HEX)
        refresh_enc = encrypt_token("ref", VALID_KEY_HEX)
        naive_expiry = datetime(2030, 6, 1, 12, 0, 0)  # no tzinfo

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, patch.dict(
            "os.environ",
            {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
        ):
            mock_db.load_tokens.return_value = {
                "access_token_enc": access_enc,
                "refresh_token_enc": refresh_enc,
                "token_expiry": naive_expiry,
                "scopes": [],
            }
            result = load_credentials(conn, "user-uuid", VALID_KEY_HEX)

        assert result.expiry.tzinfo is None
        assert result.expiry == naive_expiry

    def test_aware_expiry_is_stripped_to_naive_utc(self):
        """Aware expiry (psycopg2 returns TIMESTAMPTZ as aware) is converted to naive UTC.

        google-auth's expired property uses datetime.utcnow() (naive) for comparison;
        leaving expiry as aware raises 'can't compare offset-naive and offset-aware datetimes'.
        """
        from datetime import datetime, timezone

        access_enc = encrypt_token("tok", VALID_KEY_HEX)
        refresh_enc = encrypt_token("ref", VALID_KEY_HEX)
        aware_expiry = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, patch.dict(
            "os.environ",
            {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"},
        ):
            mock_db.load_tokens.return_value = {
                "access_token_enc": access_enc,
                "refresh_token_enc": refresh_enc,
                "token_expiry": aware_expiry,
                "scopes": [],
            }
            result = load_credentials(conn, "user-uuid", VALID_KEY_HEX)

        assert result.expiry.tzinfo is None
        assert result.expiry == datetime(2030, 6, 1, 12, 0, 0)


class TestGetService:
    """Tests for get_service — the function that crashed in production."""

    def _make_token_row(self, expiry):
        access_enc = encrypt_token("access-tok", VALID_KEY_HEX)
        refresh_enc = encrypt_token("refresh-tok", VALID_KEY_HEX)
        return {
            "access_token_enc": access_enc,
            "refresh_token_enc": refresh_enc,
            "token_expiry": expiry,
            "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        }

    def test_does_not_raise_when_expiry_is_offset_aware(self):
        """Regression: psycopg2 returns TIMESTAMPTZ as aware; get_service must not crash.

        Root cause: google-auth's creds.expired compares against datetime.utcnow()
        (naive). If creds.expiry is aware the comparison raises TypeError.
        """
        from datetime import datetime, timezone

        future_aware = datetime(2099, 1, 1, tzinfo=timezone.utc)
        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, \
             patch("claven.core.auth.build") as mock_build, \
             patch.dict("os.environ", {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"}):
            mock_db.load_tokens.return_value = self._make_token_row(future_aware)
            get_service(conn, "user-uuid", VALID_KEY_HEX)  # must not raise

    def test_refreshes_token_when_expired(self):
        """An expired token triggers a refresh and the new credentials are stored."""
        from datetime import datetime

        past_naive = datetime(2000, 1, 1)  # expired
        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, \
             patch("claven.core.auth.build"), \
             patch("claven.core.auth.Request"), \
             patch.dict("os.environ", {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"}):
            mock_db.load_tokens.return_value = self._make_token_row(past_naive)
            with patch("claven.core.auth.store_credentials") as mock_store, \
                 patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh:
                mock_refresh.return_value = None
                get_service(conn, "user-uuid", VALID_KEY_HEX)
            mock_store.assert_called_once()

    def test_refreshed_token_is_saved_in_its_own_transaction(self):
        """Saving inside the caller's long transaction deadlocked against other jobs."""
        from datetime import datetime

        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, \
             patch("claven.core.auth.build"), \
             patch("claven.core.auth.Request"), \
             patch.dict("os.environ", {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"}):
            mock_db.load_tokens.return_value = self._make_token_row(datetime(2000, 1, 1))
            token_conn = mock_db.get_connection.return_value.__enter__.return_value
            with patch("claven.core.auth.store_credentials") as mock_store, \
                 patch("google.oauth2.credentials.Credentials.refresh"):
                get_service(conn, "user-uuid", VALID_KEY_HEX)
            assert mock_store.call_args.args[0] is token_conn
            assert mock_store.call_args.args[0] is not conn

    def test_does_not_refresh_when_token_is_valid(self):
        """A non-expired token must not trigger a refresh call."""
        from datetime import datetime

        future_naive = datetime(2099, 1, 1)  # far future, not expired
        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db, \
             patch("claven.core.auth.build"), \
             patch.dict("os.environ", {"OAUTH_CLIENT_ID": "cid", "OAUTH_CLIENT_SECRET": "csec"}):
            mock_db.load_tokens.return_value = self._make_token_row(future_naive)
            with patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh:
                get_service(conn, "user-uuid", VALID_KEY_HEX)
            mock_refresh.assert_not_called()

    def test_raises_when_no_credentials(self):
        conn = MagicMock()
        with patch("claven.core.auth.db") as mock_db:
            mock_db.load_tokens.return_value = None
            with pytest.raises(ValueError, match="No credentials"):
                get_service(conn, "user-uuid", VALID_KEY_HEX)
