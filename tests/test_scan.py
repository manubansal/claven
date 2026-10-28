"""Tests for scan functions — build_known_senders and scan_inbox."""
import threading
import time as time_mod
import pytest
from unittest.mock import ANY, MagicMock, patch, call


def _make_service():
    return MagicMock()


def _make_conn():
    return MagicMock()


def _batch_metadata(**msg_map):
    """Build a fake batch_get_message_metadata return value."""
    return {msg_id: (headers, [], None) for msg_id, headers in msg_map.items()}


# ---------------------------------------------------------------------------
# _sample_batch — random desynchronization for concurrent workers
# ---------------------------------------------------------------------------

class TestSampleBatch:
    def test_returns_all_when_pool_smaller_than_batch(self):
        from claven.core.scan import _sample_batch
        candidates = [{"id": "m1"}, {"id": "m2"}]
        result = _sample_batch(candidates, 5)
        assert result == candidates

    def test_returns_all_when_pool_equals_batch(self):
        from claven.core.scan import _sample_batch
        candidates = [{"id": f"m{i}"} for i in range(5)]
        result = _sample_batch(candidates, 5)
        assert result == candidates

    def test_returns_subset_when_pool_larger_than_batch(self):
        from claven.core.scan import _sample_batch
        candidates = [{"id": f"m{i}"} for i in range(25)]
        result = _sample_batch(candidates, 5)
        assert len(result) == 5
        assert all(item in candidates for item in result)

    def test_subset_is_random_not_always_first_n(self):
        """Two samples from the same pool should differ (with high probability)."""
        from claven.core.scan import _sample_batch
        candidates = [{"id": f"m{i}"} for i in range(100)]
        sample_a = _sample_batch(candidates, 5)
        sample_b = _sample_batch(candidates, 5)
        # With 100 candidates and batch of 5, probability of identical samples
        # is ~1 in 75 million. Safe to assert they differ.
        assert sample_a != sample_b

    def test_returns_empty_for_empty_pool(self):
        from claven.core.scan import _sample_batch
        assert _sample_batch([], 5) == []


# ---------------------------------------------------------------------------
# build_known_senders — label-based sent scan
# ---------------------------------------------------------------------------

class TestBuildKnownSenders:
    """Tests for build_known_senders — queries unlabeled sent messages,
    extracts recipients, and applies sent-scanned label."""

    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_returns_zero_for_no_unscanned(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", return_value=[]), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = build_known_senders(service, conn, "u1")
        assert result == 0

    def test_inserts_recipients_into_db(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        messages = [{"id": "s1"}, {"id": "s2"}]
        metadata = _batch_metadata(
            s1={"to": "alice@example.com"},
            s2={"to": "bob@example.com"},
        )
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=2), \
             patch("claven.core.scan.db") as mock_db, \
             patch("claven.core.scan.time.sleep"):
            build_known_senders(service, conn, "u1")
        all_inserted = [addr for c in mock_db.bulk_add_known_senders.call_args_list for addr in c.args[2]]
        assert "alice@example.com" in all_inserted
        assert "bob@example.com" in all_inserted

    def test_extracts_cc_and_bcc(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        messages = [{"id": "s1"}]
        metadata = _batch_metadata(
            s1={"to": "alice@example.com", "cc": "bob@example.com", "bcc": "carol@example.com"},
        )
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.db") as mock_db, \
             patch("claven.core.scan.time.sleep"):
            build_known_senders(service, conn, "u1")
        all_inserted = [addr for c in mock_db.bulk_add_known_senders.call_args_list for addr in c.args[2]]
        assert "alice@example.com" in all_inserted
        assert "bob@example.com" in all_inserted
        assert "carol@example.com" in all_inserted

    def test_inserts_lowercase_addresses(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        messages = [{"id": "s1"}]
        metadata = _batch_metadata(s1={"to": "Alice@Example.COM"})
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.db") as mock_db, \
             patch("claven.core.scan.time.sleep"):
            build_known_senders(service, conn, "u1")
        all_inserted = [addr for c in mock_db.bulk_add_known_senders.call_args_list for addr in c.args[2]]
        assert "alice@example.com" in all_inserted
        assert "Alice@Example.COM" not in all_inserted

    def test_applies_sent_scanned_label(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        messages = [{"id": "s1"}]
        metadata = _batch_metadata(s1={"to": "a@x.com"})
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=1) as mock_apply, \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            build_known_senders(service, conn, "u1")
        pairs = mock_apply.call_args[0][1]
        assert ("s1", "Label_scanned") in pairs

    def test_stops_when_should_continue_returns_false(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", return_value=[{"id": "s1"}]), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = build_known_senders(service, conn, "u1", should_continue=lambda: False)
        assert result == 0

    def test_returns_count_of_scanned_messages(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        messages = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
        metadata = _batch_metadata(
            s1={"to": "a@x.com"}, s2={"to": "b@x.com"}, s3={"to": "c@x.com"},
        )
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=3), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = build_known_senders(service, conn, "u1")
        assert result == 3


# ---------------------------------------------------------------------------
# scan_inbox — label-based inbox scan
# ---------------------------------------------------------------------------

_LABEL_CONFIGS = [{
    "id": "known-sender",
    "name": "Known Sender",
    "unknown_label": "unknown-sender",
    "rules": [{"field": "from", "known_sender": True}],
}]
_LABEL_ID_CACHE = {"known-sender": "Label_K", "unknown-sender": "Label_U"}


class TestScanInbox:
    """Tests for scan_inbox — queries Gmail for unlabeled messages and labels them in a loop."""

    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_returns_zero_for_empty_inbox(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        with patch("claven.core.scan.list_messages", return_value=[]), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE)
        assert result == 0

    def test_labels_messages_and_returns_count(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": f"m{i}"} for i in range(3)]
        headers_map = {
            "m0": ({"from": "alice@x.com"}, [], None),
            "m1": ({"from": "bob@x.com"}, [], None),
            "m2": ({"from": "carol@x.com"}, [], None),
        }
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=3), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, known_senders=set())
        assert result == 3

    def test_applies_known_sender_label(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}]
        headers_map = {"m1": ({"from": "alice@x.com"}, [], None)}
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=1) as mock_apply, \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, known_senders={"alice@x.com"})
        pairs = mock_apply.call_args[0][1]
        assert ("m1", "Label_K") in pairs

    def test_applies_unknown_sender_label(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}]
        headers_map = {"m1": ({"from": "stranger@x.com"}, [], None)}
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=1) as mock_apply, \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, known_senders=set())
        pairs = mock_apply.call_args[0][1]
        assert ("m1", "Label_U") in pairs

    def test_updates_history_id_on_completion(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}]
        headers_map = {"m1": ({"from": "a@x.com"}, [], None)}
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "42"}), \
             patch("claven.core.scan.db") as mock_db, \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, known_senders=set())
        mock_db.set_history_id.assert_called_once_with(ANY, "u1", 42)

    def test_stops_when_should_continue_returns_false(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        with patch("claven.core.scan.list_messages", return_value=[{"id": "m1"}]), \
             patch("claven.core.scan.batch_get_message_headers"), \
             patch("claven.core.scan.batch_apply_labels"), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, should_continue=lambda: False)
        assert result == 0

    def test_retries_on_header_fetch_failure(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}]
        call_count = [0]
        def fetch_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("API error")
            return {"m1": ({"from": "a@x.com"}, [], None)}
        list_calls = [0]
        def list_side_effect(*args, **kwargs):
            list_calls[0] += 1
            return messages if list_calls[0] <= 2 else []
        with patch("claven.core.scan.list_messages", side_effect=list_side_effect), \
             patch("claven.core.scan.batch_get_message_headers", side_effect=fetch_side_effect), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            result = scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, known_senders=set())
        assert result == 1


# ---------------------------------------------------------------------------
# _unlabeled_query — scope-aware query builder
# ---------------------------------------------------------------------------

class TestUnlabeledQuery:
    def test_inbox_scope_includes_in_inbox(self):
        from claven.core.scan import _unlabeled_query
        query = _unlabeled_query(_LABEL_CONFIGS, scope="inbox")
        assert query.startswith("in:inbox ")
        assert "-label:known-sender" in query
        assert "-label:unknown-sender" in query

    def test_inbox_scope_is_default(self):
        from claven.core.scan import _unlabeled_query
        assert _unlabeled_query(_LABEL_CONFIGS) == _unlabeled_query(_LABEL_CONFIGS, scope="inbox")

    def test_allmail_scope_no_in_inbox(self):
        from claven.core.scan import _unlabeled_query
        query = _unlabeled_query(_LABEL_CONFIGS, scope="allmail")
        assert "in:inbox" not in query
        assert "-label:known-sender" in query

    def test_allmail_scope_excludes_both_labels(self):
        from claven.core.scan import _unlabeled_query
        query = _unlabeled_query(_LABEL_CONFIGS, scope="allmail")
        assert "-label:known-sender" in query
        assert "-label:unknown-sender" in query

    def test_countdown_labels_count_as_labeled(self):
        # Mail waiting in Sunset carries a countdown label instead of the unknown label.
        from claven.core.scan import _unlabeled_query
        query = _unlabeled_query(_LABEL_CONFIGS, countdown_labels=["c/24h", "c/4h"])
        assert query == "in:inbox -label:known-sender -label:unknown-sender -label:c/24h -label:c/4h"

    def test_scan_inbox_skips_mail_counting_down(self):
        from claven.core.scan import scan_inbox
        with patch("claven.core.scan.list_messages", return_value=[]) as mock_list, \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), MagicMock(), "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, countdown_labels=["c/4h"])
        assert "-label:c/4h" in mock_list.call_args.kwargs["query"]


# ---------------------------------------------------------------------------
# scan_inbox — allmail scope labels known-sender only
# ---------------------------------------------------------------------------

class TestScanInboxScope:
    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_allmail_scope_applies_both_labels(self):
        """In allmail scope, matched get known-sender, unmatched get unknown-sender."""
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}, {"id": "m2"}]
        headers_map = {
            "m1": ({"from": "alice@x.com"}, [], None),  # known
            "m2": ({"from": "stranger@x.com"}, [], None),  # unknown
        }
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=2) as mock_apply, \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE,
                       known_senders={"alice@x.com"}, scope="allmail")
        pairs = mock_apply.call_args[0][1]
        assert ("m1", "Label_K") in pairs
        assert ("m2", "Label_U") in pairs

    def test_inbox_scope_applies_both_labels(self):
        """In inbox scope, matched get known-sender, unmatched get unknown-sender."""
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}, {"id": "m2"}]
        headers_map = {
            "m1": ({"from": "alice@x.com"}, [], None),
            "m2": ({"from": "stranger@x.com"}, [], None),
        }
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=2) as mock_apply, \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE,
                       known_senders={"alice@x.com"}, scope="inbox")
        pairs = mock_apply.call_args[0][1]
        assert ("m1", "Label_K") in pairs
        assert ("m2", "Label_U") in pairs


# ---------------------------------------------------------------------------
# _notify_progress — Postgres NOTIFY for SSE
# ---------------------------------------------------------------------------

class TestNotifyProgress:
    def test_sends_pg_notify_with_json_payload(self):
        import json
        from claven.core.scan import _notify_progress
        conn = MagicMock()
        with patch("claven.core.scan.db"):
            _notify_progress(conn, "uid-1", "inbox_scan_progress", labeled=50)
        call_args = conn.cursor.return_value.__enter__.return_value.execute.call_args
        assert call_args[0][0] == "SELECT pg_notify('scan_progress', %s)"
        payload = json.loads(call_args[0][1][0])
        assert payload["user_id"] == "uid-1"
        assert payload["event"] == "inbox_scan_progress"
        assert payload["labeled"] == 50

    def test_includes_extra_data_fields(self):
        import json
        from claven.core.scan import _notify_progress
        conn = MagicMock()
        with patch("claven.core.scan.db"):
            _notify_progress(conn, "uid-1", "sent_scan_progress", scanned=100, senders=42)
        call_args = conn.cursor.return_value.__enter__.return_value.execute.call_args
        payload = json.loads(call_args[0][1][0])
        assert payload["scanned"] == 100
        assert payload["senders"] == 42


class TestScanInboxNotify:
    """Verify scan_inbox sends NOTIFY after labeling batches."""

    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_notify_sent_after_labeling(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}]
        headers_map = {"m1": ({"from": "a@x.com"}, [], None)}
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan._notify_progress") as mock_notify, \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE, known_senders=set())
        # Should have notified for batch progress + completion
        events = [c.args[2] for c in mock_notify.call_args_list]
        assert "inbox_scan_progress" in events
        assert "inbox_scan_complete" in events

    def test_no_notify_when_nothing_labeled(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        with patch("claven.core.scan.list_messages", return_value=[]), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan._notify_progress") as mock_notify, \
             patch("claven.core.scan.time.sleep"):
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE)
        # Complete event still fires even with 0 labeled
        events = [c.args[2] for c in mock_notify.call_args_list]
        assert "inbox_scan_complete" in events


class TestBuildKnownSendersNotify:
    """Verify build_known_senders sends NOTIFY after scanning batches."""

    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_notify_sent_after_batch(self):
        from claven.core.scan import build_known_senders
        service, conn = MagicMock(), MagicMock()
        messages = [{"id": "s1"}]
        metadata = _batch_metadata(s1={"to": "a@x.com"})
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.db") as mock_db, \
             patch("claven.core.scan._notify_progress") as mock_notify, \
             patch("claven.core.scan.time.sleep"):
            mock_db.count_known_senders.return_value = 1
            build_known_senders(service, conn, "u1")
        events = [c.args[2] for c in mock_notify.call_args_list]
        assert "sent_scan_progress" in events


# ---------------------------------------------------------------------------
# _interruptible_sleep — event-aware sleep for graceful shutdown
# ---------------------------------------------------------------------------

class TestInterruptibleSleep:
    def test_falls_back_to_time_sleep_without_event(self):
        from claven.core.scan import _interruptible_sleep
        with patch("claven.core.scan.time.sleep") as mock_sleep:
            _interruptible_sleep(1.5)
        mock_sleep.assert_called_once_with(1.5)

    def test_uses_event_wait_when_event_provided(self):
        from claven.core.scan import _interruptible_sleep
        event = threading.Event()
        with patch("claven.core.scan.time.sleep") as mock_sleep:
            _interruptible_sleep(0.01, shutdown_event=event)
        mock_sleep.assert_not_called()

    def test_returns_immediately_when_event_already_set(self):
        from claven.core.scan import _interruptible_sleep
        event = threading.Event()
        event.set()
        start = time_mod.monotonic()
        _interruptible_sleep(10, shutdown_event=event)
        assert time_mod.monotonic() - start < 0.1

    def test_wakes_when_event_set_during_sleep(self):
        from claven.core.scan import _interruptible_sleep
        event = threading.Event()
        timer = threading.Timer(0.05, event.set)
        timer.start()
        start = time_mod.monotonic()
        _interruptible_sleep(10, shutdown_event=event)
        elapsed = time_mod.monotonic() - start
        assert elapsed < 0.5
        timer.join()


# ---------------------------------------------------------------------------
# build_known_senders / scan_inbox — shutdown_event passthrough
# ---------------------------------------------------------------------------

class TestBuildKnownSendersShutdown:
    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_passes_shutdown_event_to_interruptible_sleep(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        messages = [{"id": "s1"}]
        metadata = _batch_metadata(s1={"to": "a@x.com"})
        event = threading.Event()
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_metadata", return_value=metadata), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan._interruptible_sleep") as mock_sleep:
            build_known_senders(service, conn, "u1", shutdown_event=event)
        # At least the 1s between-batch sleep should pass the event
        assert any(c.args[1] is event for c in mock_sleep.call_args_list if len(c.args) > 1)

    def test_error_uses_interruptible_sleep_with_event(self):
        from claven.core.scan import build_known_senders
        service, conn = _make_service(), _make_conn()
        event = threading.Event()
        call_count = [0]
        def fail_then_empty(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{"id": "s1"}]
            return []
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned"), \
             patch("claven.core.scan.list_messages", side_effect=fail_then_empty), \
             patch("claven.core.scan.batch_get_message_metadata", side_effect=Exception("API error")), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan._interruptible_sleep") as mock_sleep:
            build_known_senders(service, conn, "u1", shutdown_event=event)
        # Error backoff should use 5s with the event
        assert any(c.args == (5, event) for c in mock_sleep.call_args_list)


class TestScanInboxShutdown:
    def _once_then_empty(self, messages):
        calls = [0]
        def side_effect(*args, **kwargs):
            calls[0] += 1
            return messages if calls[0] == 1 else []
        return side_effect

    def test_passes_shutdown_event_to_interruptible_sleep(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        messages = [{"id": "m1"}]
        headers_map = {"m1": ({"from": "a@x.com"}, [], None)}
        event = threading.Event()
        with patch("claven.core.scan.list_messages", side_effect=self._once_then_empty(messages)), \
             patch("claven.core.scan.batch_get_message_headers", return_value=headers_map), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan._interruptible_sleep") as mock_sleep:
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE,
                       known_senders=set(), shutdown_event=event)
        assert any(c.args[1] is event for c in mock_sleep.call_args_list if len(c.args) > 1)

    def test_error_uses_interruptible_sleep_with_event(self):
        from claven.core.scan import scan_inbox
        conn = MagicMock()
        event = threading.Event()
        call_count = [0]
        def fail_then_succeed(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("API error")
            return {"m1": ({"from": "a@x.com"}, [], None)}
        list_calls = [0]
        def list_side(*args, **kwargs):
            list_calls[0] += 1
            return [{"id": "m1"}] if list_calls[0] <= 2 else []
        with patch("claven.core.scan.list_messages", side_effect=list_side), \
             patch("claven.core.scan.batch_get_message_headers", side_effect=fail_then_succeed), \
             patch("claven.core.scan.batch_apply_labels", return_value=1), \
             patch("claven.core.scan.get_profile", return_value={"historyId": "99"}), \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan._interruptible_sleep") as mock_sleep:
            scan_inbox(MagicMock(), conn, "u1", _LABEL_CONFIGS, _LABEL_ID_CACHE,
                       known_senders=set(), shutdown_event=event)
        assert any(c.args == (5, event) for c in mock_sleep.call_args_list)


# ---------------------------------------------------------------------------
# Label names come from config / claven.core.labels, not hardcoded strings
# ---------------------------------------------------------------------------

class TestLabelNames:
    def test_sent_scan_uses_short_hidden_marker(self):
        from claven.core.scan import SENT_SCANNED_LABEL, build_known_senders
        assert SENT_SCANNED_LABEL == "c/s"
        service, conn = _make_service(), _make_conn()
        with patch("claven.core.scan.ensure_label_exists", return_value="Label_scanned") as mock_ensure, \
             patch("claven.core.scan.list_messages", return_value=[]) as mock_list, \
             patch("claven.core.scan.db"), \
             patch("claven.core.scan.time.sleep"):
            build_known_senders(service, conn, "u1")
        mock_ensure.assert_called_once_with(service, "c/s")
        assert "-label:c/s" in str(mock_list.call_args)

    def test_relabel_searches_configured_unknown_label(self):
        from claven.core.scan import relabel_scan
        configs = [{"id": "c/k", "unknown_label": "c/u", "rules": []}]
        service = MagicMock()
        service.users().messages().list().execute.return_value = {"messages": []}
        with patch("claven.core.scan.db") as mock_db:
            mock_db.get_pending_relabel_senders.return_value = ["alice@example.com"]
            relabel_scan(service, "u1", configs, {"c/k": "LK", "c/u": "LU"})
        assert service.users().messages().list.call_args.kwargs["q"] == "from:alice@example.com label:c/u"

    def test_relabel_moves_counting_down_mail_to_known(self):
        # In Sunset, a sender's waiting mail carries a countdown label instead of c/u.
        from claven.core.scan import relabel_scan
        configs = [{"id": "c/k", "unknown_label": "c/u", "rules": []}]
        service = MagicMock()
        service.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
        with patch("claven.core.scan.db") as mock_db, \
             patch("claven.core.scan.batch_swap_labels", return_value=1) as mock_swap, \
             patch("claven.core.scan._interruptible_sleep"):
            mock_db.get_pending_relabel_senders.return_value = ["alice@example.com"]
            relabel_scan(service, "u1", configs, {"c/k": "LK", "c/u": "LU"}, countdown_ids={"c/4h": "L4"})
        assert service.users().messages().list.call_args.kwargs["q"] == "from:alice@example.com {label:c/u label:c/4h}"
        mock_swap.assert_called_once_with(service, ["m1"], "LU", "LK", also_remove=["L4"])

    def test_config_uses_short_codes(self):
        import yaml
        from pathlib import Path
        config = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
        assert config["labels"][0]["id"] == "c/k"
        assert config["labels"][0]["unknown_label"] == "c/u"
