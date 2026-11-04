"""Account deletion: remove everything Claven holds for a user, at their request.

Google is told first (stop the push watch, revoke the OAuth grant), best
effort, then the users row is deleted; every other table cascades from it.
"""

import logging

import requests

import claven.core.auth as auth
import claven.core.db as db
from claven.core.watch import stop_watch

logger = logging.getLogger(__name__)

REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def revoke_token(token: str) -> bool:
    """Revoke Claven's OAuth grant with Google. Returns True if Google confirmed it."""
    try:
        response = requests.post(REVOKE_URL, data={"token": token}, timeout=10)
    except requests.RequestException as exc:
        logger.warning("Google token revoke failed: %s", type(exc).__name__)
        return False
    if response.status_code != 200:
        logger.warning("Google token revoke returned %s", response.status_code)
        return False
    logger.debug("revoke_token: revoked")
    return True


def delete_account(conn, user_id: str, key_hex: str) -> dict:
    """Delete the user and all their data, after stopping their watch and revoking access.

    Failures talking to Google are logged and never block the deletion.
    """
    # Committed first so this user's background scans and jobs see it and stop.
    db.set_cancel_state(conn, user_id, "cancel_job")
    conn.commit()

    watch_stopped = False
    token_revoked = False
    row = db.load_tokens(conn, user_id)
    if row is None:
        logger.debug("delete_account: no Gmail tokens for %s", user_id)
    elif row.get("revoked_at"):
        logger.debug("delete_account: access for %s already revoked, skipping Google", user_id)
    else:
        logger.debug("delete_account: stopping watch and revoking access for %s", user_id)
        try:
            stop_watch(auth.get_service(conn, user_id, key_hex))
            watch_stopped = True
        except Exception as exc:
            logger.warning("stop_watch failed during account deletion for %s: %s", user_id, type(exc).__name__)
        try:
            refresh_token = auth.decrypt_token(bytes(row["refresh_token_enc"]), key_hex)
        except Exception as exc:
            logger.warning("Could not decrypt refresh token for %s: %s", user_id, type(exc).__name__)
        else:
            token_revoked = revoke_token(refresh_token)

    db.delete_user(conn, user_id)
    logger.info("Account deleted for %s (watch_stopped=%s, token_revoked=%s)", user_id, watch_stopped, token_revoked,
                extra={"event": "account_deleted", "user_id": user_id})
    return {"watch_stopped": watch_stopped, "token_revoked": token_revoked}
