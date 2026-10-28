"""Tests for core logic — process_message, poll_new_messages, initial_scan."""
import pytest
from unittest.mock import ANY, MagicMock, patch, call


# ---------------------------------------------------------------------------
# process_message
# ---------------------------------------------------------------------------

_NEWSLETTER_CONFIG = {
    "id": "newsletter",
    "name": "Newsletter",
    "rules": [{"field": "from", "contains": ["newsletter"]}],
}
_KNOWN_SENDER_CONFIG = {
    "id": "known-sender",
    "name": "Known Sender",
    "unknown_label": "unknown-sender",
    "rules": [{"field": "from", "known_sender": True}],
}


class TestProcessMessage:
    def test_applies_matching_label(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "newsletter@example.com"}, ["INBOX"], None)
            label_configs = [_NEWSLETTER_CONFIG]
            label_id_cache = {"newsletter": "Label_123"}
            process_message(service, "msg1", label_configs, label_id_cache)
            mock_apply.assert_called_once_with(service, "msg1", "Label_123")

    def test_does_not_apply_label_already_present(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            # Label already on the message
            mock_headers.return_value = ({"from": "newsletter@example.com"}, ["INBOX", "Label_123"], None)
            label_configs = [_NEWSLETTER_CONFIG]
            label_id_cache = {"newsletter": "Label_123"}
            process_message(service, "msg1", label_configs, label_id_cache)
            mock_apply.assert_not_called()

    def test_no_matching_labels(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "friend@example.com"}, ["INBOX"], None)
            # Config with no unknown_label and no match → nothing applied
            label_configs = [{"id": "newsletter", "name": "Newsletter", "rules": [{"field": "from", "contains": ["newsletter"]}]}]
            process_message(service, "msg1", label_configs, {})
            mock_apply.assert_not_called()

    def test_returns_early_if_no_headers(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = (None, [], None)
            process_message(service, "msg1", [_NEWSLETTER_CONFIG], {"newsletter": "Label_123"})
            mock_apply.assert_not_called()

    def test_applies_multiple_matching_label_configs(self):
        from claven.core.process import process_message
        service = MagicMock()
        finance_config = {"id": "finance", "name": "Finance", "rules": [{"field": "subject", "contains": ["invoice"]}]}
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "newsletter@example.com", "subject": "Invoice #1"}, ["INBOX"], None)
            label_configs = [_NEWSLETTER_CONFIG, finance_config]
            label_id_cache = {"newsletter": "Label_1", "finance": "Label_2"}
            process_message(service, "msg1", label_configs, label_id_cache)
            assert mock_apply.call_count == 2
            mock_apply.assert_any_call(service, "msg1", "Label_1")
            mock_apply.assert_any_call(service, "msg1", "Label_2")

    def test_skips_label_not_in_cache(self):
        # Label matched by rules but not in label_id_cache — silently skipped
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "newsletter@example.com"}, ["INBOX"], None)
            process_message(service, "msg1", [_NEWSLETTER_CONFIG], {})  # empty cache
            mock_apply.assert_not_called()

    def test_applies_unknown_label_when_no_rule_matches(self):
        # When no rule matches and unknown_label is set, apply the unknown label
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "stranger@example.com"}, ["INBOX"], None)
            label_id_cache = {"known-sender": "Label_known", "unknown-sender": "Label_unknown"}
            process_message(service, "msg1", [_KNOWN_SENDER_CONFIG], label_id_cache, known_senders=set())
            mock_apply.assert_called_once_with(service, "msg1", "Label_unknown")

    def test_does_not_apply_unknown_label_when_not_configured(self):
        # No unknown_label in config → nothing applied on no-match
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "stranger@example.com"}, ["INBOX"], None)
            label_configs = [{"id": "newsletter", "name": "Newsletter", "rules": [{"field": "from", "contains": ["newsletter"]}]}]
            process_message(service, "msg1", label_configs, {"newsletter": "Label_123"})
            mock_apply.assert_not_called()

    def test_passes_known_senders_to_rule_matching(self):
        from claven.core.process import process_message
        service = MagicMock()
        known_senders = {"alice@example.com"}
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "alice@example.com"}, ["INBOX"], None)
            label_id_cache = {"known-sender": "Label_known", "unknown-sender": "Label_unknown"}
            process_message(service, "msg1", [_KNOWN_SENDER_CONFIG], label_id_cache, known_senders=known_senders)
            # alice is a known sender → should get known-sender label, not unknown-sender
            mock_apply.assert_called_once_with(service, "msg1", "Label_known")

    def test_returns_true_when_label_applied(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label"):
            mock_headers.return_value = ({"from": "newsletter@example.com"}, ["INBOX"], None)
            result = process_message(service, "msg1", [_NEWSLETTER_CONFIG], {"newsletter": "Label_123"})
        assert result is True

    def test_returns_false_when_label_already_present(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label"):
            mock_headers.return_value = ({"from": "newsletter@example.com"}, ["INBOX", "Label_123"], None)
            result = process_message(service, "msg1", [_NEWSLETTER_CONFIG], {"newsletter": "Label_123"})
        assert result is False

    def test_returns_false_when_no_headers(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers:
            mock_headers.return_value = (None, [], None)
            result = process_message(service, "msg1", [_NEWSLETTER_CONFIG], {"newsletter": "Label_123"})
        assert result is False

    def test_returns_false_when_no_match_and_no_unknown_label(self):
        from claven.core.process import process_message
        service = MagicMock()
        with patch("claven.core.process.get_message_metadata") as mock_headers, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_headers.return_value = ({"from": "friend@example.com"}, ["INBOX"], None)
            result = process_message(service, "msg1", [_NEWSLETTER_CONFIG], {"newsletter": "Label_123"})
        assert result is False
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# poll_new_messages
# ---------------------------------------------------------------------------

class TestPollNewMessages:
    def test_processes_new_inbox_message(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            mock_history.return_value = [{
                "messagesAdded": [{"message": {"id": "msg1", "labelIds": ["INBOX"]}}]
            }]
            result = poll_new_messages(service, "history123", [], {})
            mock_process.assert_called_once_with(service, "msg1", [], {}, None, auto_archive_unknown=False, sunset=None)
            assert result == 1

    def test_ignores_non_inbox_messages(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            mock_history.return_value = [{
                "messagesAdded": [{"message": {"id": "msg1", "labelIds": ["SENT"]}}]
            }]
            poll_new_messages(service, "history123", [], {})
            mock_process.assert_not_called()

    def test_returns_none_on_expired_history_id(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history:
            mock_history.side_effect = Exception("404: historyId is invalid")
            result = poll_new_messages(service, "expired_id", [], {})
            assert result is None

    def test_returns_zero_when_no_new_messages(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            mock_history.return_value = []
            result = poll_new_messages(service, "history123", [], {})
            mock_process.assert_not_called()
            assert result == 0

    def test_processes_multiple_new_messages(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            mock_history.return_value = [{
                "messagesAdded": [
                    {"message": {"id": "msg1", "labelIds": ["INBOX"]}},
                    {"message": {"id": "msg2", "labelIds": ["INBOX"]}},
                ]
            }]
            poll_new_messages(service, "history123", [], {})
            assert mock_process.call_count == 2

    def test_deduplicates_messages_across_history_records(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            # Same message ID appears in two history records
            mock_history.return_value = [
                {"messagesAdded": [{"message": {"id": "msg1", "labelIds": ["INBOX"]}}]},
                {"messagesAdded": [{"message": {"id": "msg1", "labelIds": ["INBOX"]}}]},
            ]
            poll_new_messages(service, "history123", [], {})
            assert mock_process.call_count == 1

    def test_passes_known_senders(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        known_senders = {"alice@example.com"}
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            mock_history.return_value = [{
                "messagesAdded": [{"message": {"id": "msg1", "labelIds": ["INBOX"]}}]
            }]
            poll_new_messages(service, "history123", [], {}, known_senders=known_senders)
            mock_process.assert_called_once_with(service, "msg1", [], {}, known_senders, auto_archive_unknown=False, sunset=None)

    def test_reraises_non_404_exceptions(self):
        from claven.core.process import poll_new_messages
        service = MagicMock()
        with patch("claven.core.process.list_history") as mock_history:
            mock_history.side_effect = Exception("500: Internal Server Error")
            with pytest.raises(Exception, match="500"):
                poll_new_messages(service, "history123", [], {})


# ---------------------------------------------------------------------------
# initial_scan
# ---------------------------------------------------------------------------

class TestInitialScan:
    def setup_method(self):
        # Ensure the global running flag is True before each test
        import claven.core.scan as scan_module
        scan_module.running = True

    def test_processes_all_pending_messages(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        messages = [{"id": "msg1"}, {"id": "msg2"}, {"id": "msg3"}]
        with patch("claven.core.scan.list_messages", return_value=messages), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(set(), 0)), \
             patch("claven.core.scan.process_message") as mock_process, \
             patch("claven.core.scan.save_scan_checkpoint"):
            initial_scan(service, str(tmp_path), [], {})
            assert mock_process.call_count == 3

    def test_skips_already_processed_messages(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        messages = [{"id": "msg1"}, {"id": "msg2"}, {"id": "msg3"}]
        already_processed = {"msg1", "msg2"}
        with patch("claven.core.scan.list_messages", return_value=messages), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(already_processed, 0)), \
             patch("claven.core.scan.process_message") as mock_process, \
             patch("claven.core.scan.save_scan_checkpoint"):
            initial_scan(service, str(tmp_path), [], {})
            assert mock_process.call_count == 1
            mock_process.assert_called_once_with(service, "msg3", [], {}, None)

    def test_reprocesses_all_when_known_senders_grew(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        messages = [{"id": "msg1"}, {"id": "msg2"}]
        already_processed = {"msg1", "msg2"}
        # known_senders_count in checkpoint is 5; current known_senders has 10
        known_senders = set(f"user{i}@example.com" for i in range(10))
        with patch("claven.core.scan.list_messages", return_value=messages), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(already_processed, 5)), \
             patch("claven.core.scan.process_message") as mock_process, \
             patch("claven.core.scan.save_scan_checkpoint"):
            initial_scan(service, str(tmp_path), [], {}, known_senders=known_senders)
            assert mock_process.call_count == 2  # all reprocessed

    def test_does_not_reprocess_when_known_senders_unchanged(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        messages = [{"id": "msg1"}, {"id": "msg2"}]
        already_processed = {"msg1", "msg2"}
        known_senders = set(f"user{i}@example.com" for i in range(5))
        with patch("claven.core.scan.list_messages", return_value=messages), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(already_processed, 5)), \
             patch("claven.core.scan.process_message") as mock_process, \
             patch("claven.core.scan.save_scan_checkpoint"):
            initial_scan(service, str(tmp_path), [], {}, known_senders=known_senders)
            mock_process.assert_not_called()

    def test_respects_max_messages(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        with patch("claven.core.scan.list_messages") as mock_list, \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(set(), 0)), \
             patch("claven.core.scan.process_message"), \
             patch("claven.core.scan.save_scan_checkpoint"):
            mock_list.return_value = [{"id": "msg1"}, {"id": "msg2"}]
            initial_scan(service, str(tmp_path), [], {}, max_messages=2)
            mock_list.assert_called_once_with(service, query="in:inbox", max_results=2)

    def test_saves_checkpoint_on_completion(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        messages = [{"id": "msg1"}, {"id": "msg2"}]
        with patch("claven.core.scan.list_messages", return_value=messages), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(set(), 0)), \
             patch("claven.core.scan.process_message"), \
             patch("claven.core.scan.save_scan_checkpoint") as mock_save:
            initial_scan(service, str(tmp_path), [], {})
            assert mock_save.called

    def test_stops_when_running_is_false(self, tmp_path):
        import claven.core.scan as scan_module
        from claven.core.scan import initial_scan
        service = MagicMock()
        messages = [{"id": f"msg{i}"} for i in range(20)]
        call_count = 0

        def fake_process(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                scan_module.running = False

        with patch("claven.core.scan.list_messages", return_value=messages), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(set(), 0)), \
             patch("claven.core.scan.process_message", side_effect=fake_process), \
             patch("claven.core.scan.save_scan_checkpoint"):
            initial_scan(service, str(tmp_path), [], {})
            assert call_count < len(messages)

    def test_no_messages_to_process(self, tmp_path):
        from claven.core.scan import initial_scan
        service = MagicMock()
        with patch("claven.core.scan.list_messages", return_value=[]), \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(set(), 0)), \
             patch("claven.core.scan.process_message") as mock_process, \
             patch("claven.core.scan.save_scan_checkpoint"):
            initial_scan(service, str(tmp_path), [], {})
            mock_process.assert_not_called()

    def test_max_messages_none_does_not_crash(self, tmp_path):
        # Regression: 513013a fixed a format crash when max_messages=None was
        # passed directly to the log string. Verify None is handled cleanly.
        from claven.core.scan import initial_scan
        service = MagicMock()
        with patch("claven.core.scan.list_messages") as mock_list, \
             patch("claven.core.scan.load_scan_checkpoint", return_value=(set(), 0)), \
             patch("claven.core.scan.process_message"), \
             patch("claven.core.scan.save_scan_checkpoint"):
            mock_list.return_value = []
            initial_scan(service, str(tmp_path), [], {}, max_messages=None)
            mock_list.assert_called_once_with(service, query="in:inbox", max_results=None)


# ---------------------------------------------------------------------------
# Sunset: first countdown label at arrival
# ---------------------------------------------------------------------------

class TestSunsetCountdownOnArrival:
    """In Sunset mode, live labeling adds the starting countdown with c/u."""

    _CONFIGS = [{"id": "c/k", "unknown_label": "c/u", "rules": [{"field": "from", "known_sender": True}]}]
    _CACHE = {"c/k": "LK", "c/u": "LU"}

    def _sunset(self, hours=24):
        from claven.core.sunset import COUNTDOWN_LABELS
        return {"hours": hours, "changed_at": None,
                "label_ids": {name: f"id-{name}" for name in COUNTDOWN_LABELS}}

    def _process(self, labels, sender="stranger@example.com", sunset="default", arrived_ago_min=1):
        from datetime import datetime, timedelta, timezone
        from claven.core.process import process_message
        sunset = self._sunset() if sunset == "default" else sunset
        arrived = datetime.now(timezone.utc) - timedelta(minutes=arrived_ago_min)
        with patch("claven.core.process.get_message_metadata") as mock_meta, \
             patch("claven.core.process.apply_label") as mock_apply:
            mock_meta.return_value = ({"from": sender}, labels, int(arrived.timestamp() * 1000))
            process_message(MagicMock(), "m1", self._CONFIGS, self._CACHE, {"friend@example.com"}, sunset=sunset)
        return mock_apply

    def test_unread_unknown_inbox_mail_gets_countdown_in_same_call(self):
        mock_apply = self._process(["INBOX", "UNREAD"])
        # 24h timer, arrived a minute ago: gone within 24h.
        mock_apply.assert_called_once_with(ANY, "m1", "LU", "id-c/24h")

    def test_three_day_timer_starts_at_3d_step(self):
        mock_apply = self._process(["INBOX", "UNREAD"], sunset=self._sunset(hours=72))
        mock_apply.assert_called_once_with(ANY, "m1", "LU", "id-c/3d")

    def test_late_processed_mail_gets_step_for_actual_time_left(self):
        # Arrived 21h ago (e.g. push missed, picked up by the poll): 3h left.
        mock_apply = self._process(["INBOX", "UNREAD"], arrived_ago_min=21 * 60)
        mock_apply.assert_called_once_with(ANY, "m1", "LU", "id-c/4h")

    @pytest.mark.parametrize("labels", [["INBOX"], ["INBOX", "UNREAD", "STARRED"], ["UNREAD"]])
    def test_read_starred_or_archived_mail_gets_no_countdown(self, labels):
        mock_apply = self._process(labels)
        mock_apply.assert_called_once_with(ANY, "m1", "LU")

    def test_known_sender_gets_no_countdown(self):
        mock_apply = self._process(["INBOX", "UNREAD"], sender="friend@example.com")
        mock_apply.assert_called_once_with(ANY, "m1", "LK")

    def test_other_modes_get_no_countdown(self):
        mock_apply = self._process(["INBOX", "UNREAD"], sunset=None)
        mock_apply.assert_called_once_with(ANY, "m1", "LU")

    def test_poll_passes_sunset_through(self):
        from claven.core.process import poll_new_messages
        sunset = self._sunset()
        with patch("claven.core.process.list_history") as mock_history, \
             patch("claven.core.process.process_message") as mock_process:
            mock_history.return_value = [{"messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]}]
            poll_new_messages(MagicMock(), "h1", [], {}, sunset=sunset)
        assert mock_process.call_args.kwargs["sunset"] is sunset
