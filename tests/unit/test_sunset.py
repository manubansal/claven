"""Tests for claven.core.sunset — timed archiving of unread unknown-sender mail."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from claven.core.sunset import (
    INBOX_MODES,
    SUNSET_HOURS_CHOICES,
    archive_expired_unknown,
    sunset_cutoff,
    sunset_query,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class TestConstants:
    def test_modes(self):
        assert INBOX_MODES == ("label_only", "gatekeeper", "sunset")

    def test_hour_choices_include_default(self):
        assert 24 in SUNSET_HOURS_CHOICES


class TestSunsetCutoff:
    def test_cutoff_is_now_minus_timer(self):
        changed = NOW - timedelta(days=10)
        assert sunset_cutoff(NOW, 24, changed) == NOW - timedelta(hours=24)

    def test_no_cutoff_until_timer_has_run_since_mode_change(self):
        # Mail already in the inbox when Sunset was turned on gets a full timer,
        # so switching modes never archives the existing backlog at once.
        changed = NOW - timedelta(hours=23)
        assert sunset_cutoff(NOW, 24, changed) is None

    def test_cutoff_once_timer_elapsed_since_mode_change(self):
        changed = NOW - timedelta(hours=24, minutes=1)
        assert sunset_cutoff(NOW, 24, changed) == NOW - timedelta(hours=24)

    def test_missing_changed_at_uses_now_minus_timer(self):
        assert sunset_cutoff(NOW, 4, None) == NOW - timedelta(hours=4)


class TestSunsetQuery:
    def test_query_targets_unread_unstarred_unknown_inbox_mail_before_cutoff(self):
        cutoff = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        q = sunset_query("unknown-sender", cutoff)
        assert q == f"in:inbox is:unread -is:starred label:unknown-sender before:{int(cutoff.timestamp())}"


def _list_pages(*pages):
    """Mock messages().list().execute() returning the given pages of ids."""
    results = []
    for i, ids in enumerate(pages):
        page = {"messages": [{"id": m} for m in ids]}
        if i < len(pages) - 1:
            page["nextPageToken"] = f"t{i + 1}"
        results.append(page)
    return results


class TestArchiveExpiredUnknown:
    def test_archives_all_matching_messages_across_pages(self):
        service = MagicMock()
        service.users().messages().list().execute.side_effect = _list_pages(["a", "b"], ["c"])
        cutoff = NOW - timedelta(hours=24)
        with patch("claven.core.sunset.batch_remove_labels", return_value=3) as mock_remove:
            count = archive_expired_unknown(service, "unknown-sender", cutoff)
        assert count == 3
        mock_remove.assert_called_once_with(service, ["a", "b", "c"], ["INBOX"])
        list_kwargs = service.users().messages().list.call_args_list[-1].kwargs
        assert list_kwargs["q"] == sunset_query("unknown-sender", cutoff)
        assert list_kwargs["pageToken"] == "t1"

    def test_nothing_to_archive(self):
        service = MagicMock()
        service.users().messages().list().execute.return_value = {}
        with patch("claven.core.sunset.batch_remove_labels") as mock_remove:
            count = archive_expired_unknown(service, "unknown-sender", NOW)
        assert count == 0
        mock_remove.assert_not_called()

    def test_chunks_by_batch_limit(self):
        service = MagicMock()
        ids = [f"m{i}" for i in range(250)]
        service.users().messages().list().execute.return_value = {"messages": [{"id": m} for m in ids]}
        with patch("claven.core.sunset.batch_remove_labels", side_effect=lambda s, b, l: len(b)) as mock_remove, \
                patch("claven.core.sunset._BATCH_LIMIT", 100):
            count = archive_expired_unknown(service, "unknown-sender", NOW)
        assert count == 250
        assert [len(c.args[1]) for c in mock_remove.call_args_list] == [100, 100, 50]
