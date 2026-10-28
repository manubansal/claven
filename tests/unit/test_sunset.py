"""Tests for claven.core.sunset — timed archiving of unread unknown-sender mail."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from claven.core.sunset import (
    INBOX_MODES,
    unknown_group,
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


UNKNOWN_GROUP = "{label:unknown-sender label:c/3d label:c/24h label:c/12h label:c/4h label:c/1h}"


class TestSunsetQuery:
    def test_query_targets_unread_unstarred_unknown_inbox_mail_before_cutoff(self):
        # Waiting mail carries a countdown label instead of the unknown label.
        cutoff = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        q = sunset_query("unknown-sender", cutoff)
        assert q == f"in:inbox is:unread -is:starred {UNKNOWN_GROUP} before:{int(cutoff.timestamp())}"


def _list_pages(*pages):
    """Mock messages().list().execute() returning the given pages of ids."""
    results = []
    for i, ids in enumerate(pages):
        page = {"messages": [{"id": m} for m in ids]}
        if i < len(pages) - 1:
            page["nextPageToken"] = f"t{i + 1}"
        results.append(page)
    return results


class TestUnknownGroup:
    def test_unknown_label_or_any_countdown(self):
        assert unknown_group("unknown-sender") == UNKNOWN_GROUP

    def test_only_listed_countdowns(self):
        # Searching a label that doesn't exist is avoided by naming only existing ones.
        assert unknown_group("c/u", ["c/4h"]) == "{label:c/u label:c/4h}"

    def test_no_countdowns_is_plain_label(self):
        assert unknown_group("c/u", []) == "label:c/u"


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


# ── Countdown labels ─────────────────────────────────────────────────────────

from claven.core.sunset import (  # noqa: E402
    COUNTDOWN_LABELS,
    clear_countdowns,
    countdown_label,
    message_expiry,
    update_countdowns,
)


class TestCountdownLabel:
    def test_labels_are_short_codes_under_c(self):
        assert COUNTDOWN_LABELS == ("c/3d", "c/24h", "c/12h", "c/4h", "c/1h")

    @pytest.mark.parametrize("hours,label", [
        (80, "c/3d"),
        (72, "c/3d"),       # fresh mail on a 3-day timer shows the full timer
        (71.9, "c/3d"),
        (24.01, "c/3d"),
        (24, "c/24h"),      # fresh mail on a 24h timer shows c/24h
        (12.01, "c/24h"),
        (12, "c/12h"),
        (4.01, "c/12h"),
        (4, "c/4h"),        # "c/4h" means gone within 4 hours
        (1.01, "c/4h"),
        (1, "c/1h"),
        (0.5, "c/1h"),
        (-1, "c/1h"),       # overdue until the next sweep archives it
    ])
    def test_rounds_time_left_up_to_a_step(self, hours, label):
        assert countdown_label(timedelta(hours=hours)) == label


class TestMessageExpiry:
    def test_expiry_is_arrival_plus_timer(self):
        arrived = NOW - timedelta(hours=2)
        ms = int(arrived.timestamp() * 1000)
        assert message_expiry(ms, 24, NOW - timedelta(days=5)) == arrived + timedelta(hours=24)

    def test_mail_older_than_mode_switch_counts_from_switch(self):
        switched = NOW - timedelta(hours=1)
        ms = int((NOW - timedelta(days=3)).timestamp() * 1000)
        assert message_expiry(ms, 24, switched) == switched + timedelta(hours=24)


def _ids_by(service, mapping):
    """messages().list().execute() → ids for a query or labelIds, keyed by mapping."""
    def _list(**kwargs):
        key = kwargs.get("q") or tuple(kwargs.get("labelIds", []))
        call = MagicMock()
        call.execute.return_value = {"messages": [{"id": m} for m in mapping.get(key, [])]}
        return call
    service.users.return_value.messages.return_value.list.side_effect = _list


LABEL_IDS = {name: f"L{i}" for i, name in enumerate(COUNTDOWN_LABELS)}  # L0=3d … L4=1h


class TestUpdateCountdowns:
    """A waiting message carries exactly one countdown label and no c/u; the rest carry c/u."""

    def _run(self, waiting, metadata, labeled=None):
        service = MagicMock()
        mapping = {f"in:inbox is:unread -is:starred {UNKNOWN_GROUP}": waiting}
        for lid, ids in (labeled or {}).items():
            mapping[(lid,)] = ids
        _ids_by(service, mapping)
        with patch("claven.core.sunset.batch_get_message_metadata", return_value=metadata), \
                patch("claven.core.sunset.batch_modify_labels") as mock_modify:
            changed = update_countdowns(service, "unknown-sender", "LU", NOW, 24, NOW - timedelta(days=5), LABEL_IDS)
        return changed, mock_modify

    def test_swaps_unknown_for_the_right_step(self):
        ms = int((NOW - timedelta(hours=14)).timestamp() * 1000)  # 10h left → gone within 12h
        changed, mock_modify = self._run(["m1"], {"m1": ({}, ["INBOX", "LU"], ms)})
        assert changed == 1
        call = mock_modify.call_args_list[0]
        assert call.args[1] == ["m1"]
        assert call.kwargs["add_label_ids"] == ["L2"]
        assert set(call.kwargs["remove_label_ids"]) == {"LU", "L0", "L1", "L3", "L4"}

    def test_strips_unknown_from_mail_already_on_the_right_step(self):
        # Mail labeled before countdowns replaced c/u carries both.
        ms = int((NOW - timedelta(hours=14)).timestamp() * 1000)
        changed, mock_modify = self._run(["m1"], {"m1": ({}, ["INBOX", "LU", "L2"], ms)}, labeled={"L2": ["m1"]})
        assert changed == 1
        assert mock_modify.call_args_list[0].kwargs["add_label_ids"] == ["L2"]
        assert "LU" in mock_modify.call_args_list[0].kwargs["remove_label_ids"]

    def test_leaves_messages_already_on_the_right_step(self):
        ms = int((NOW - timedelta(hours=14)).timestamp() * 1000)
        changed, mock_modify = self._run(["m1"], {"m1": ({}, ["INBOX", "L2"], ms)}, labeled={"L2": ["m1"]})
        assert changed == 0
        mock_modify.assert_not_called()

    def test_mail_no_longer_waiting_goes_back_to_unknown(self):
        # m2 still carries a countdown label but was read, starred, or archived.
        changed, mock_modify = self._run([], {}, labeled={"L3": ["m2"]})
        mock_modify.assert_called_once()
        call = mock_modify.call_args
        assert call.args[1] == ["m2"]
        assert call.kwargs["add_label_ids"] == ["LU"]
        assert set(call.kwargs["remove_label_ids"]) == set(LABEL_IDS.values())


class TestClearCountdowns:
    def test_deletes_existing_countdown_labels(self):
        service = MagicMock()
        service.users().labels().list().execute.return_value = {"labels": [
            {"id": "L3", "name": "c/4h"},
            {"id": "X", "name": "c/u"},
        ]}
        _ids_by(service, {})
        with patch("claven.core.sunset.batch_modify_labels"):
            assert clear_countdowns(service, "X") == 1
        service.users().labels().delete.assert_called_with(userId="me", id="L3")

    def test_marks_waiting_mail_unknown_before_deleting(self):
        # Deleting a countdown label strips it from its mail, which must stay marked unknown.
        service = MagicMock()
        service.users().labels().list().execute.return_value = {"labels": [{"id": "L3", "name": "c/4h"}]}
        _ids_by(service, {("L3",): ["m1", "m2"]})
        with patch("claven.core.sunset.batch_modify_labels") as mock_modify:
            clear_countdowns(service, "X")
        mock_modify.assert_called_once_with(service, ["m1", "m2"], add_label_ids=["X"])

    def test_also_deletes_countdown_labels_with_old_names(self):
        service = MagicMock()
        service.users().labels().list().execute.return_value = {"labels": [
            {"id": "L3", "name": "sunset/4h left"},
            {"id": "L9", "name": "c/12h"},
            {"id": "X", "name": "c/u"},
        ]}
        _ids_by(service, {})
        with patch("claven.core.sunset.batch_modify_labels"):
            assert clear_countdowns(service, "X") == 2


class TestExistingCountdownIds:
    def test_returns_only_countdown_labels_that_exist(self):
        from claven.core.sunset import existing_countdown_ids
        service = MagicMock()
        service.users().labels().list().execute.return_value = {"labels": [
            {"id": "L3", "name": "c/4h"},
            {"id": "X", "name": "c/u"},
        ]}
        assert existing_countdown_ids(service) == {"c/4h": "L3"}
        service.users().labels().create.assert_not_called()
