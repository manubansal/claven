"""OAuth token encryption and credential management.

Fernet symmetric encryption keeps raw access/refresh tokens out of the DB.
The encryption key (TOKEN_ENCRYPTION_KEY env var) is a 64-char hex string
representing 32 bytes — generated once by scripts/setup-gcp.sh.
"""

import base64
import logging
import os
from datetime import timezone

from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import claven.core.db as db

logger = logging.getLogger(__name__)


class GmailAccessRevoked(Exception):
    """Google no longer accepts the user's refresh token; they must sign in again.

    newly_revoked is True when this call discovered it, False when it was
    already known and Google wasn't contacted.
    """

    def __init__(self, user_id: str, newly_revoked: bool):
        super().__init__(f"Gmail access revoked for user {user_id}; waiting for them to sign in again")
        self.newly_revoked = newly_revoked


def _get_fernet(key_hex: str) -> Fernet:
    key_bytes = bytes.fromhex(key_hex)  # 32 bytes
    key_b64 = base64.urlsafe_b64encode(key_bytes)  # Fernet requires url-safe base64
    return Fernet(key_b64)


def encrypt_token(plaintext: str, key_hex: str) -> bytes:
    return _get_fernet(key_hex).encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes, key_hex: str) -> str:
    return _get_fernet(key_hex).decrypt(ciphertext).decode()


def store_credentials(conn, user_id: str, creds: Credentials, key_hex: str) -> None:
    access_enc = encrypt_token(creds.token, key_hex)
    refresh_enc = encrypt_token(creds.refresh_token, key_hex)
    scopes = list(creds.scopes or [])
    db.store_tokens(conn, user_id, access_enc, refresh_enc, creds.expiry, scopes)


def load_credentials(conn, user_id: str, key_hex: str) -> Credentials | None:
    row = db.load_tokens(conn, user_id)
    if not row:
        return None
    return _credentials_from_row(row, key_hex)


def _credentials_from_row(row: dict, key_hex: str) -> Credentials:
    access_token = decrypt_token(bytes(row["access_token_enc"]), key_hex)
    refresh_token = decrypt_token(bytes(row["refresh_token_enc"]), key_hex)
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ.get("OAUTH_CLIENT_ID"),
        client_secret=os.environ.get("OAUTH_CLIENT_SECRET"),
        scopes=row.get("scopes") or [],
    )
    expiry = row.get("token_expiry")
    if expiry is not None and expiry.tzinfo is not None:
        # google-auth's expired property compares against datetime.utcnow() (naive);
        # strip tzinfo so the comparison doesn't raise an offset-aware/naive error.
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    creds.expiry = expiry
    return creds


def get_service(conn, user_id: str, key_hex: str):
    """Load credentials, refresh if needed, and return a Gmail API service.

    Raises GmailAccessRevoked when Google has rejected the refresh token, now
    or on an earlier call.
    """
    row = db.load_tokens(conn, user_id)
    if not row:
        raise ValueError(f"No credentials found for user {user_id}")
    if row.get("revoked_at"):
        logger.debug("get_service: access for %s revoked since %s", user_id, row["revoked_at"])
        raise GmailAccessRevoked(user_id, newly_revoked=False)
    creds = _credentials_from_row(row, key_hex)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            if "invalid_grant" not in str(exc):
                logger.debug("get_service: refresh failed for %s, not a revoked grant", user_id)
                raise
            logger.debug("get_service: refresh token rejected for %s, marking access revoked", user_id)
            # Own transaction: the caller's rolls back when this error propagates.
            with db.get_connection() as revoke_conn:
                db.mark_gmail_access_revoked(revoke_conn, user_id)
            raise GmailAccessRevoked(user_id, newly_revoked=True) from exc
        logger.debug("get_service: refreshed token for %s", user_id)
        # Commit right away, outside the caller's transaction: holding the token
        # row while the caller goes on to lock scan_state deadlocked concurrent jobs.
        with db.get_connection() as token_conn:
            store_credentials(token_conn, user_id, creds, key_hex)
    return build("gmail", "v1", credentials=creds)
