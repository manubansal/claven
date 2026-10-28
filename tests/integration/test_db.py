"""Integration tests for claven/core/db.py — require a real Postgres database.

Run with: PYTEST_DATABASE_URL=postgresql://... pytest -m integration
"""

from datetime import datetime, timezone

import pytest

import claven.core.db as db

pytestmark = pytest.mark.integration


class TestUpsertUser:
    def test_creates_new_user(self, db_conn):
        user_id = db.upsert_user(db_conn, "new@example.com")
        assert user_id is not None
        assert isinstance(user_id, str)

    def test_returns_same_id_for_duplicate_email(self, db_conn):
        id1 = db.upsert_user(db_conn, "dup@example.com")
        id2 = db.upsert_user(db_conn, "dup@example.com")
        assert id1 == id2

    def test_different_emails_get_different_ids(self, db_conn):
        id1 = db.upsert_user(db_conn, "alice@example.com")
        id2 = db.upsert_user(db_conn, "bob@example.com")
        assert id1 != id2


class TestGetUser:
    def test_get_user_by_email_found(self, db_conn):
        user_id = db.upsert_user(db_conn, "lookup@example.com")
        user = db.get_user_by_email(db_conn, "lookup@example.com")
        assert user is not None
        assert user["email"] == "lookup@example.com"
        assert user["id"] == user_id

    def test_get_user_by_email_missing_returns_none(self, db_conn):
        result = db.get_user_by_email(db_conn, "missing@example.com")
        assert result is None

    def test_get_all_users_includes_created_users(self, db_conn):
        db.upsert_user(db_conn, "ua@example.com")
        db.upsert_user(db_conn, "ub@example.com")
        users = db.get_all_users(db_conn)
        emails = {u["email"] for u in users}
        assert "ua@example.com" in emails
        assert "ub@example.com" in emails


class TestTokens:
    def test_store_and_load_round_trip(self, db_conn):
        user_id = db.upsert_user(db_conn, "tokens@example.com")
        db.store_tokens(db_conn, user_id, b"access-enc", b"refresh-enc", None, ["scope1"])
        row = db.load_tokens(db_conn, user_id)
        assert row is not None
        assert bytes(row["access_token_enc"]) == b"access-enc"
        assert bytes(row["refresh_token_enc"]) == b"refresh-enc"
        assert row["scopes"] == ["scope1"]

    def test_load_tokens_missing_returns_none(self, db_conn):
        user_id = db.upsert_user(db_conn, "notokens@example.com")
        assert db.load_tokens(db_conn, user_id) is None

    def test_store_tokens_overwrites_existing(self, db_conn):
        user_id = db.upsert_user(db_conn, "overwrite@example.com")
        db.store_tokens(db_conn, user_id, b"old-access", b"old-refresh", None, [])
        db.store_tokens(db_conn, user_id, b"new-access", b"new-refresh", None, ["s"])
        row = db.load_tokens(db_conn, user_id)
        assert bytes(row["access_token_enc"]) == b"new-access"
        assert bytes(row["refresh_token_enc"]) == b"new-refresh"


class TestScanState:
    def test_set_and_get_history_id(self, db_conn):
        user_id = db.upsert_user(db_conn, "history@example.com")
        db.set_history_id(db_conn, user_id, 12345)
        assert db.get_history_id(db_conn, user_id) == 12345

    def test_get_history_id_missing_returns_none(self, db_conn):
        user_id = db.upsert_user(db_conn, "nohistory@example.com")
        assert db.get_history_id(db_conn, user_id) is None

    def test_set_history_id_overwrites(self, db_conn):
        user_id = db.upsert_user(db_conn, "updatehist@example.com")
        db.set_history_id(db_conn, user_id, 100)
        db.set_history_id(db_conn, user_id, 200)
        assert db.get_history_id(db_conn, user_id) == 200


class TestProcessedCount:
    def test_set_and_get_processed_count(self, db_conn):
        user_id = db.upsert_user(db_conn, "proc@example.com")
        db.set_history_id(db_conn, user_id, 1)  # create scan_state row
        db.set_processed_count(db_conn, user_id, 42)
        assert db.get_processed_count(db_conn, user_id) == 42

    def test_set_overwrites_not_increments(self, db_conn):
        user_id = db.upsert_user(db_conn, "procoverwrite@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_processed_count(db_conn, user_id, 100)
        db.set_processed_count(db_conn, user_id, 50)
        assert db.get_processed_count(db_conn, user_id) == 50

    def test_increment_processed_count(self, db_conn):
        user_id = db.upsert_user(db_conn, "procinc@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.increment_processed_count(db_conn, user_id, 10)
        db.increment_processed_count(db_conn, user_id, 5)
        assert db.get_processed_count(db_conn, user_id) == 15


class TestInboxScanCompleted:
    def test_default_is_false(self, db_conn):
        user_id = db.upsert_user(db_conn, "inboxdefault@example.com")
        db.set_history_id(db_conn, user_id, 1)
        assert db.is_inbox_scan_completed(db_conn, user_id) is False

    def test_set_and_check(self, db_conn):
        user_id = db.upsert_user(db_conn, "inboxcomplete@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_inbox_scan_completed(db_conn, user_id)
        assert db.is_inbox_scan_completed(db_conn, user_id) is True

    def test_no_row_returns_false(self, db_conn):
        user_id = db.upsert_user(db_conn, "inboxnorow@example.com")
        assert db.is_inbox_scan_completed(db_conn, user_id) is False


class TestSentScanProgress:
    def test_default_progress(self, db_conn):
        user_id = db.upsert_user(db_conn, "sentdefault@example.com")
        progress = db.get_sent_scan_progress(db_conn, user_id)
        assert progress["messages_scanned"] == 0
        assert progress["messages_total"] is None
        assert progress["status"] is None

    def test_set_and_get_progress(self, db_conn):
        user_id = db.upsert_user(db_conn, "sentprog@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_sent_scan_progress(db_conn, user_id, 500, 1000)
        progress = db.get_sent_scan_progress(db_conn, user_id)
        assert progress["messages_scanned"] == 500
        assert progress["messages_total"] == 1000

    def test_set_status(self, db_conn):
        user_id = db.upsert_user(db_conn, "sentstatus@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_sent_scan_status(db_conn, user_id, "in_progress")
        assert db.get_sent_scan_progress(db_conn, user_id)["status"] == "in_progress"
        db.set_sent_scan_status(db_conn, user_id, "complete")
        assert db.get_sent_scan_progress(db_conn, user_id)["status"] == "complete"

    def test_updated_at_is_set(self, db_conn):
        user_id = db.upsert_user(db_conn, "sentupdated@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_sent_scan_status(db_conn, user_id, "in_progress")
        progress = db.get_sent_scan_progress(db_conn, user_id)
        assert progress["updated_at"] is not None


class TestTryLockUserScan:
    def test_lock_succeeds_when_row_exists(self, db_conn):
        user_id = db.upsert_user(db_conn, "lockable@example.com")
        db.set_history_id(db_conn, user_id, 1)
        assert db.try_lock_user_scan(db_conn, user_id) is True

    def test_creates_row_and_locks_when_no_row(self, db_conn):
        user_id = db.upsert_user(db_conn, "norow@example.com")
        # No scan_state row — ensure_scan_state creates it, then lock succeeds
        assert db.try_lock_user_scan(db_conn, user_id) is True

    def test_second_connection_skips_locked_row(self, db_url):
        """Two connections: first acquires lock, second gets False."""
        import psycopg2
        conn1 = psycopg2.connect(db_url)
        conn2 = psycopg2.connect(db_url)
        conn1.autocommit = False
        conn2.autocommit = False
        try:
            # Create user and scan_state on conn1
            with conn1.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (email) VALUES ('lockrace@example.com') RETURNING id::text"
                )
                user_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO scan_state (user_id, history_id) VALUES (%s, 1)",
                    (user_id,),
                )
            conn1.commit()

            # conn1 acquires lock (inside a transaction)
            assert db.try_lock_user_scan(conn1, user_id) is True

            # conn2 should be skipped (SKIP LOCKED)
            assert db.try_lock_user_scan(conn2, user_id) is False

            # After conn1 commits, conn2 can acquire
            conn1.commit()
            assert db.try_lock_user_scan(conn2, user_id) is True
        finally:
            conn1.rollback()
            conn2.rollback()
            # Clean up
            conn1.autocommit = True
            with conn1.cursor() as cur:
                cur.execute("DELETE FROM users WHERE email = 'lockrace@example.com'")
            conn1.close()
            conn2.close()


class TestGetUserById:
    def test_found(self, db_conn):
        user_id = db.upsert_user(db_conn, "byid@example.com")
        user = db.get_user_by_id(db_conn, user_id)
        assert user is not None
        assert user["email"] == "byid@example.com"
        assert user["id"] == user_id

    def test_missing_returns_none(self, db_conn):
        import uuid
        assert db.get_user_by_id(db_conn, str(uuid.uuid4())) is None


class TestDeleteCredentials:
    def test_removes_tokens_and_scan_state(self, db_conn):
        user_id = db.upsert_user(db_conn, "disconnect@example.com")
        db.store_tokens(db_conn, user_id, b"a-enc", b"r-enc", None, [])
        db.set_history_id(db_conn, user_id, 999)

        db.delete_credentials(db_conn, user_id)

        assert db.load_tokens(db_conn, user_id) is None
        assert db.get_history_id(db_conn, user_id) is None

    def test_keeps_user_row(self, db_conn):
        user_id = db.upsert_user(db_conn, "keepuser@example.com")
        db.store_tokens(db_conn, user_id, b"a-enc", b"r-enc", None, [])
        db.delete_credentials(db_conn, user_id)
        assert db.get_user_by_email(db_conn, "keepuser@example.com") is not None

    def test_noop_when_no_credentials(self, db_conn):
        user_id = db.upsert_user(db_conn, "nocreds@example.com")
        db.delete_credentials(db_conn, user_id)  # must not raise


class TestSentRecipients:
    def test_add_and_get_known_senders(self, db_conn):
        user_id = db.upsert_user(db_conn, "senders@example.com")
        db.add_known_sender(db_conn, user_id, "alice@test.com")
        senders = db.get_known_senders(db_conn, user_id)
        assert "alice@test.com" in senders

    def test_add_known_sender_idempotent(self, db_conn):
        user_id = db.upsert_user(db_conn, "idem@example.com")
        db.add_known_sender(db_conn, user_id, "bob@test.com")
        db.add_known_sender(db_conn, user_id, "bob@test.com")  # no error
        senders = db.get_known_senders(db_conn, user_id)
        # No duplicates — it's a set
        assert senders == {"bob@test.com"}

    def test_bulk_add_known_senders(self, db_conn):
        user_id = db.upsert_user(db_conn, "bulk@example.com")
        db.bulk_add_known_senders(db_conn, user_id, ["x@a.com", "y@b.com", "z@c.com"])
        assert db.get_known_senders(db_conn, user_id) == {"x@a.com", "y@b.com", "z@c.com"}

    def test_bulk_add_empty_list_is_noop(self, db_conn):
        user_id = db.upsert_user(db_conn, "emptyrecip@example.com")
        db.bulk_add_known_senders(db_conn, user_id, [])  # must not raise
        assert db.get_known_senders(db_conn, user_id) == set()

    def test_get_known_senders_empty(self, db_conn):
        user_id = db.upsert_user(db_conn, "nosenders@example.com")
        assert db.get_known_senders(db_conn, user_id) == set()


class TestInboxMode:
    def test_new_connection_defaults_to_gatekeeper_24h(self, db_conn):
        user_id = db.upsert_user(db_conn, "mode-default@example.com")
        db.set_history_id(db_conn, user_id, 1)
        mode = db.get_inbox_mode(db_conn, user_id)
        assert mode["mode"] == "gatekeeper"
        assert mode["sunset_hours"] == 24

    def test_no_scan_state_returns_label_only(self, db_conn):
        user_id = db.upsert_user(db_conn, "mode-norow@example.com")
        assert db.get_inbox_mode(db_conn, user_id)["mode"] == "label_only"

    def test_set_sunset_with_hours(self, db_conn):
        user_id = db.upsert_user(db_conn, "mode-sunset@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_inbox_mode(db_conn, user_id, "sunset", sunset_hours=12)
        mode = db.get_inbox_mode(db_conn, user_id)
        assert mode["mode"] == "sunset"
        assert mode["sunset_hours"] == 12
        assert mode["changed_at"] is not None

    def test_changed_at_only_moves_when_mode_changes(self, db_conn):
        user_id = db.upsert_user(db_conn, "mode-changed@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_inbox_mode(db_conn, user_id, "sunset")
        with db_conn.cursor() as cur:
            cur.execute("UPDATE scan_state SET inbox_mode_changed_at = '2026-01-01T00:00:00Z' WHERE user_id = %s", (user_id,))
        pinned = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.set_inbox_mode(db_conn, user_id, "sunset", sunset_hours=4)  # same mode, new timer
        assert db.get_inbox_mode(db_conn, user_id)["changed_at"] == pinned
        db.set_inbox_mode(db_conn, user_id, "gatekeeper")
        assert db.get_inbox_mode(db_conn, user_id)["changed_at"] > pinned

    def test_auto_archive_unknown_is_derived_from_mode(self, db_conn):
        user_id = db.upsert_user(db_conn, "mode-derived@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_inbox_mode(db_conn, user_id, "sunset")
        assert db.get_auto_archive_unknown(db_conn, user_id) is False
        db.set_inbox_mode(db_conn, user_id, "gatekeeper")
        assert db.get_auto_archive_unknown(db_conn, user_id) is True

    def test_set_auto_archive_maps_to_modes(self, db_conn):
        user_id = db.upsert_user(db_conn, "mode-legacy@example.com")
        db.set_history_id(db_conn, user_id, 1)
        db.set_auto_archive_unknown(db_conn, user_id, False)
        assert db.get_inbox_mode(db_conn, user_id)["mode"] == "label_only"
        db.set_auto_archive_unknown(db_conn, user_id, True)
        assert db.get_inbox_mode(db_conn, user_id)["mode"] == "gatekeeper"

    def test_get_sunset_users(self, db_conn):
        sunset_id = db.upsert_user(db_conn, "mode-list-sunset@example.com")
        db.set_history_id(db_conn, sunset_id, 1)
        db.set_inbox_mode(db_conn, sunset_id, "sunset", sunset_hours=72)
        gate_id = db.upsert_user(db_conn, "mode-list-gate@example.com")
        db.set_history_id(db_conn, gate_id, 1)
        users = {u["user_id"]: u for u in db.get_sunset_users(db_conn)}
        assert sunset_id in users
        assert gate_id not in users
        assert users[sunset_id]["sunset_hours"] == 72
        assert users[sunset_id]["changed_at"] is not None
