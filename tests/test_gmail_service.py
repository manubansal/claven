"""Tests for claven.core.gmail — file-based state and Gmail API wrappers."""
import pytest
from unittest.mock import MagicMock, call
from claven.core.gmail import (
    _parse_addresses,
    load_scan_checkpoint,
    save_scan_checkpoint,
    _load_recipients_cache,
    _save_recipients_cache,
    get_message_headers,
    ensure_label_exists,
    apply_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mock_service():
    """Return a MagicMock that mimics the Gmail API service fluent interface."""
    return MagicMock()


# ---------------------------------------------------------------------------
# _parse_addresses
# ---------------------------------------------------------------------------

def test_parse_addresses_bare_email():
    assert _parse_addresses("alice@example.com") == ["alice@example.com"]


def test_parse_addresses_display_name_format():
    assert _parse_addresses("Alice Smith <alice@example.com>") == ["alice@example.com"]


def test_parse_addresses_multiple_comma_separated():
    result = _parse_addresses("alice@example.com, bob@example.com")
    assert "alice@example.com" in result
    assert "bob@example.com" in result


def test_parse_addresses_empty_string():
    assert _parse_addresses("") == []


def test_parse_addresses_no_email():
    assert _parse_addresses("no email here") == []


def test_parse_addresses_mixed_formats():
    result = _parse_addresses("Alice <alice@example.com>, bob@example.com")
    assert "alice@example.com" in result
    assert "bob@example.com" in result


# ---------------------------------------------------------------------------
# load_scan_checkpoint / save_scan_checkpoint
# ---------------------------------------------------------------------------

def test_load_scan_checkpoint_returns_empty_when_missing(tmp_path):
    ids, count = load_scan_checkpoint(str(tmp_path))
    assert ids == set()
    assert count == 0


def test_save_load_scan_checkpoint_roundtrip(tmp_path):
    ids = {"msg1", "msg2", "msg3"}
    save_scan_checkpoint(ids, str(tmp_path), known_senders_count=42)
    loaded_ids, loaded_count = load_scan_checkpoint(str(tmp_path))
    assert loaded_ids == ids
    assert loaded_count == 42


def test_save_load_scan_checkpoint_zero_senders(tmp_path):
    save_scan_checkpoint({"msg1"}, str(tmp_path), known_senders_count=0)
    _, count = load_scan_checkpoint(str(tmp_path))
    assert count == 0


def test_save_load_scan_checkpoint_empty_ids(tmp_path):
    save_scan_checkpoint(set(), str(tmp_path), known_senders_count=5)
    ids, count = load_scan_checkpoint(str(tmp_path))
    assert ids == set()
    assert count == 5


def test_save_scan_checkpoint_overwrites_previous(tmp_path):
    save_scan_checkpoint({"msg1"}, str(tmp_path), known_senders_count=1)
    save_scan_checkpoint({"msg2", "msg3"}, str(tmp_path), known_senders_count=2)
    ids, count = load_scan_checkpoint(str(tmp_path))
    assert ids == {"msg2", "msg3"}
    assert count == 2


# ---------------------------------------------------------------------------
# _load_recipients_cache / _save_recipients_cache
# ---------------------------------------------------------------------------

def test_load_recipients_cache_returns_empty_when_missing(tmp_path):
    recipients, history_id, resume_index = _load_recipients_cache(str(tmp_path))
    assert recipients == set()
    assert history_id is None
    assert resume_index == 0


def test_save_load_recipients_cache_roundtrip(tmp_path):
    recipients = {"alice@example.com", "bob@example.com"}
    _save_recipients_cache(recipients, "history123", str(tmp_path), resume_index=5)
    loaded_recipients, loaded_history_id, loaded_index = _load_recipients_cache(str(tmp_path))
    assert loaded_recipients == recipients
    assert loaded_history_id == "history123"
    assert loaded_index == 5


def test_save_load_recipients_cache_no_history_id(tmp_path):
    _save_recipients_cache({"alice@example.com"}, None, str(tmp_path))
    _, history_id, _ = _load_recipients_cache(str(tmp_path))
    assert history_id is None


def test_save_load_recipients_cache_zero_resume_index(tmp_path):
    _save_recipients_cache({"alice@example.com"}, "h1", str(tmp_path), resume_index=0)
    _, _, resume_index = _load_recipients_cache(str(tmp_path))
    assert resume_index == 0


# ---------------------------------------------------------------------------
# get_message_headers
# ---------------------------------------------------------------------------

def test_get_message_headers_extracts_all_fields():
    service = mock_service()
    service.users().messages().get().execute.return_value = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "To", "value": "bob@example.com"},
            ]
        },
        "labelIds": ["INBOX", "UNREAD"],
    }
    headers, label_ids = get_message_headers(service, "msg123")
    assert headers["from"] == "Alice <alice@example.com>"
    assert headers["to"] == "bob@example.com"
    assert "INBOX" in label_ids
    assert "UNREAD" in label_ids


def test_get_message_metadata_returns_arrival_time():
    from claven.core.gmail import get_message_metadata
    service = mock_service()
    service.users().messages().get().execute.return_value = {
        "payload": {"headers": [{"name": "From", "value": "a@example.com"}]},
        "labelIds": ["INBOX"],
        "internalDate": "1761645600000",
    }
    headers, label_ids, internal_date_ms = get_message_metadata(service, "m1")
    assert headers == {"from": "a@example.com"}
    assert label_ids == ["INBOX"]
    assert internal_date_ms == 1761645600000


def test_apply_label_adds_several_labels_in_one_call():
    service = mock_service()
    apply_label(service, "m1", "LU", "L12")
    service.users().messages().modify.assert_called_with(
        userId="me", id="m1", body={"addLabelIds": ["LU", "L12"]})


def test_get_message_headers_never_requests_subject():
    # Data minimization: no label rule uses the subject, so it is never fetched.
    service = mock_service()
    service.users().messages().get().execute.return_value = {
        "payload": {"headers": [{"name": "Subject", "value": "Hello there"}]},
        "labelIds": [],
    }
    headers, _ = get_message_headers(service, "msg1")
    requested = service.users().messages().get.call_args.kwargs["metadataHeaders"]
    assert "Subject" not in requested
    assert "subject" not in headers


def test_get_message_headers_returns_lowercase_keys():
    service = mock_service()
    service.users().messages().get().execute.return_value = {
        "payload": {"headers": [{"name": "FROM", "value": "test@example.com"}]},
        "labelIds": [],
    }
    headers, _ = get_message_headers(service, "msg1")
    assert "from" in headers
    assert "FROM" not in headers


def test_get_message_headers_missing_headers_returns_empty():
    service = mock_service()
    service.users().messages().get().execute.return_value = {
        "payload": {"headers": []},
        "labelIds": [],
    }
    headers, label_ids = get_message_headers(service, "msg1")
    assert headers == {}
    assert label_ids == []


def test_get_message_headers_ignores_irrelevant_headers():
    service = mock_service()
    service.users().messages().get().execute.return_value = {
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Date", "value": "Mon, 1 Jan 2024"},
                {"name": "X-Custom", "value": "ignored"},
            ]
        },
        "labelIds": [],
    }
    headers, _ = get_message_headers(service, "msg1")
    assert "from" in headers
    assert "date" not in headers
    assert "x-custom" not in headers


# ---------------------------------------------------------------------------
# ensure_label_exists
# ---------------------------------------------------------------------------

def test_ensure_label_exists_returns_existing_id():
    service = mock_service()
    service.users().labels().list().execute.return_value = {
        "labels": [{"name": "Newsletter", "id": "Label_123"}]
    }
    label_id = ensure_label_exists(service, "Newsletter")
    assert label_id == "Label_123"


def test_ensure_label_exists_creates_when_missing():
    service = mock_service()
    service.users().labels().list().execute.return_value = {"labels": []}
    service.users().labels().create().execute.return_value = {
        "name": "Newsletter", "id": "Label_456"
    }
    label_id = ensure_label_exists(service, "Newsletter")
    assert label_id == "Label_456"


def test_ensure_label_exists_case_insensitive_match():
    service = mock_service()
    service.users().labels().list().execute.return_value = {
        "labels": [{"name": "NEWSLETTER", "id": "Label_123"}]
    }
    label_id = ensure_label_exists(service, "newsletter")
    assert label_id == "Label_123"


def test_ensure_label_exists_does_not_create_if_exists():
    service = mock_service()
    service.users().labels().list().execute.return_value = {
        "labels": [{"name": "Newsletter", "id": "Label_123"}]
    }
    ensure_label_exists(service, "Newsletter")
    service.users().labels().create.assert_not_called()


# ---------------------------------------------------------------------------
# apply_label
# ---------------------------------------------------------------------------

def test_apply_label_calls_modify_with_correct_args():
    service = mock_service()
    apply_label(service, "msg123", "Label_123")
    service.users().messages().modify.assert_called_once_with(
        userId="me",
        id="msg123",
        body={"addLabelIds": ["Label_123"]},
    )


def test_apply_label_calls_execute():
    service = mock_service()
    apply_label(service, "msg1", "Label_1")
    service.users().messages().modify().execute.assert_called_once()


# ---------------------------------------------------------------------------
# list_messages — pagination
# ---------------------------------------------------------------------------

def test_list_messages_single_page():
    from claven.core.gmail import list_messages
    service = mock_service()
    service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "msg1"}, {"id": "msg2"}],
    }
    result = list_messages(service, max_results=None)
    assert [m["id"] for m in result] == ["msg1", "msg2"]


def test_list_messages_paginates():
    from claven.core.gmail import list_messages
    service = mock_service()
    service.users().messages().list.return_value.execute.side_effect = [
        {"messages": [{"id": "msg1"}, {"id": "msg2"}], "nextPageToken": "token1"},
        {"messages": [{"id": "msg3"}]},
    ]
    result = list_messages(service, max_results=None)
    assert [m["id"] for m in result] == ["msg1", "msg2", "msg3"]


def test_list_messages_empty_inbox():
    from claven.core.gmail import list_messages
    service = mock_service()
    service.users().messages().list.return_value.execute.return_value = {}
    result = list_messages(service, max_results=None)
    assert result == []


def test_list_messages_sends_max_results_as_batch_size():
    # list_messages passes max_results as maxResults to the API and stops
    # requesting more pages once it has at least max_results messages.
    # It does NOT truncate the response — if the API returns more than
    # max_results in one page, all returned messages are included.
    # In practice the Gmail API respects maxResults, so this is not an issue.
    from claven.core.gmail import list_messages
    service = mock_service()
    service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "msg1"}, {"id": "msg2"}],
        # No nextPageToken — stops after one page
    }
    result = list_messages(service, max_results=2)
    assert len(result) == 2


def test_list_messages_stops_requesting_pages_once_max_results_reached():
    # Once len(messages) >= max_results the while condition exits — no further
    # pages are fetched, even if a nextPageToken was present.
    from claven.core.gmail import list_messages
    service = mock_service()
    service.users().messages().list.return_value.execute.side_effect = [
        {"messages": [{"id": "msg1"}, {"id": "msg2"}], "nextPageToken": "token1"},
        # Second page should never be fetched
        {"messages": [{"id": "msg3"}, {"id": "msg4"}]},
    ]
    result = list_messages(service, max_results=2)
    # Exactly 2 messages fetched; second page never requested
    assert [m["id"] for m in result] == ["msg1", "msg2"]


# ---------------------------------------------------------------------------
# list_history — pagination
# ---------------------------------------------------------------------------

def test_list_history_single_page():
    from claven.core.gmail import list_history
    service = mock_service()
    mock_request = MagicMock()
    mock_request.execute.return_value = {"history": [{"id": "h1"}, {"id": "h2"}]}
    service.users().history().list.return_value = mock_request
    service.users().history().list_next.return_value = None

    result = list_history(service, "start_id")
    assert [r["id"] for r in result] == ["h1", "h2"]


def test_list_history_paginates():
    from claven.core.gmail import list_history
    service = mock_service()
    mock_request1 = MagicMock()
    mock_request2 = MagicMock()
    mock_request1.execute.return_value = {"history": [{"id": "h1"}]}
    mock_request2.execute.return_value = {"history": [{"id": "h2"}]}
    service.users().history().list.return_value = mock_request1
    service.users().history().list_next.side_effect = [mock_request2, None]

    result = list_history(service, "start_id")
    assert [r["id"] for r in result] == ["h1", "h2"]


def test_list_history_empty():
    from claven.core.gmail import list_history
    service = mock_service()
    mock_request = MagicMock()
    mock_request.execute.return_value = {}
    service.users().history().list.return_value = mock_request
    service.users().history().list_next.return_value = None

    result = list_history(service, "start_id")
    assert result == []


# ---------------------------------------------------------------------------
# _extract_recipients_from_messages
# ---------------------------------------------------------------------------

def test_extract_recipients_returns_addresses_and_not_interrupted():
    from claven.core.gmail import _extract_recipients_from_messages
    service = mock_service()
    messages = [{"id": "m1"}, {"id": "m2"}]
    service.users().messages().get.return_value.execute.side_effect = [
        {"payload": {"headers": [{"name": "To", "value": "alice@example.com"}]}},
        {"payload": {"headers": [{"name": "To", "value": "bob@example.com"}]}},
    ]
    recipients, interrupted = _extract_recipients_from_messages(service, messages)
    assert "alice@example.com" in recipients
    assert "bob@example.com" in recipients
    assert interrupted is False


def test_extract_recipients_empty_messages():
    from claven.core.gmail import _extract_recipients_from_messages
    service = mock_service()
    recipients, interrupted = _extract_recipients_from_messages(service, [])
    assert recipients == set()
    assert interrupted is False


def test_extract_recipients_interrupted_returns_partial_and_flag():
    from claven.core.gmail import _extract_recipients_from_messages
    service = mock_service()
    messages = [{"id": f"m{i}"} for i in range(5)]
    call_count = 0

    def stop_after_two():
        nonlocal call_count
        call_count += 1
        return call_count <= 2

    service.users().messages().get.return_value.execute.return_value = {
        "payload": {"headers": [{"name": "To", "value": "alice@example.com"}]}
    }
    recipients, interrupted = _extract_recipients_from_messages(
        service, messages, should_continue=stop_after_two
    )
    assert interrupted is True
    # Processed fewer than all 5 messages
    assert service.users().messages().get.call_count < 5


def test_extract_recipients_no_should_continue_processes_all():
    from claven.core.gmail import _extract_recipients_from_messages
    service = mock_service()
    messages = [{"id": f"m{i}"} for i in range(3)]
    service.users().messages().get.return_value.execute.return_value = {
        "payload": {"headers": [{"name": "To", "value": "alice@example.com"}]}
    }
    recipients, interrupted = _extract_recipients_from_messages(service, messages)
    assert interrupted is False
    assert service.users().messages().get.call_count == 3


def test_extract_recipients_extracts_cc_and_bcc():
    from claven.core.gmail import _extract_recipients_from_messages
    service = mock_service()
    service.users().messages().get.return_value.execute.return_value = {
        "payload": {"headers": [
            {"name": "To", "value": "alice@example.com"},
            {"name": "Cc", "value": "bob@example.com"},
            {"name": "Bcc", "value": "charlie@example.com"},
        ]}
    }
    recipients, _ = _extract_recipients_from_messages(service, [{"id": "m1"}])
    assert recipients == {"alice@example.com", "bob@example.com", "charlie@example.com"}


# ---------------------------------------------------------------------------
# Batch functions
# ---------------------------------------------------------------------------

class TestBatchGetMessageMetadata:
    def test_returns_headers_for_successful_messages(self):
        from claven.core.gmail import batch_get_message_metadata
        service = MagicMock()

        def fake_execute():
            # Simulate the batch callback being called for each request
            callback = service.new_batch_http_request.call_args[1]["callback"]
            callback("m1", {"payload": {"headers": [{"name": "From", "value": "a@x.com"}]}, "labelIds": ["INBOX"]}, None)
            callback("m2", {"payload": {"headers": [{"name": "From", "value": "b@x.com"}]}, "labelIds": []}, None)

        batch_mock = MagicMock()
        batch_mock.execute.side_effect = fake_execute
        service.new_batch_http_request.return_value = batch_mock

        result = batch_get_message_metadata(service, ["m1", "m2"], ["From"])
        assert "m1" in result
        assert "m2" in result
        assert result["m1"][0] == {"from": "a@x.com"}
        assert result["m2"][0] == {"from": "b@x.com"}

    def test_omits_failed_messages(self):
        from claven.core.gmail import batch_get_message_metadata
        service = MagicMock()

        def fake_execute():
            callback = service.new_batch_http_request.call_args[1]["callback"]
            callback("m1", {"payload": {"headers": [{"name": "From", "value": "a@x.com"}]}, "labelIds": []}, None)
            callback("m2", None, Exception("429 rate limit"))

        batch_mock = MagicMock()
        batch_mock.execute.side_effect = fake_execute
        service.new_batch_http_request.return_value = batch_mock

        result = batch_get_message_metadata(service, ["m1", "m2"], ["From"], max_retries=0)
        assert "m1" in result
        assert "m2" not in result

    def test_respects_custom_headers(self):
        from claven.core.gmail import batch_get_message_metadata
        service = MagicMock()

        def fake_execute():
            callback = service.new_batch_http_request.call_args[1]["callback"]
            callback("m1", {"payload": {"headers": [
                {"name": "To", "value": "a@x.com"},
                {"name": "Cc", "value": "b@x.com"},
                {"name": "From", "value": "sender@x.com"},  # not requested
            ]}, "labelIds": []}, None)

        batch_mock = MagicMock()
        batch_mock.execute.side_effect = fake_execute
        service.new_batch_http_request.return_value = batch_mock

        result = batch_get_message_metadata(service, ["m1"], ["To", "Cc"])
        headers = result["m1"][0]
        assert "to" in headers
        assert "cc" in headers
        assert "from" not in headers  # not in requested headers


class TestBatchGetMessageHeaders:
    def test_never_requests_subject(self):
        from claven.core.gmail import batch_get_message_headers
        service = MagicMock()
        service.new_batch_http_request.return_value = MagicMock()
        batch_get_message_headers(service, ["m1"])
        requested = service.users().messages().get.call_args.kwargs["metadataHeaders"]
        assert "From" in requested
        assert "Subject" not in requested

    def test_metadata_default_never_requests_subject(self):
        from claven.core.gmail import batch_get_message_metadata
        service = MagicMock()
        service.new_batch_http_request.return_value = MagicMock()
        batch_get_message_metadata(service, ["m1"])
        requested = service.users().messages().get.call_args.kwargs["metadataHeaders"]
        assert "Subject" not in requested


class TestBatchApplyLabels:
    def test_returns_count_of_successful_applications(self):
        from claven.core.gmail import batch_apply_labels
        service = MagicMock()

        def fake_execute():
            callback = service.new_batch_http_request.call_args[1]["callback"]
            callback("m1", {}, None)
            callback("m2", {}, None)
            callback("m3", None, Exception("error"))

        batch_mock = MagicMock()
        batch_mock.execute.side_effect = fake_execute
        service.new_batch_http_request.return_value = batch_mock

        result = batch_apply_labels(service, [("m1", "L1"), ("m2", "L1"), ("m3", "L1")], max_retries=0)
        assert result == 2  # m3 failed

    def test_returns_zero_when_all_fail(self):
        from claven.core.gmail import batch_apply_labels
        service = MagicMock()

        def fake_execute():
            callback = service.new_batch_http_request.call_args[1]["callback"]
            callback("m1", None, Exception("error"))

        batch_mock = MagicMock()
        batch_mock.execute.side_effect = fake_execute
        service.new_batch_http_request.return_value = batch_mock

        result = batch_apply_labels(service, [("m1", "L1")], max_retries=0)
        assert result == 0


class TestBatchModifyLabels:
    def test_chunks_of_1000_with_add_and_remove(self):
        from claven.core.gmail import batch_modify_labels
        service = MagicMock()
        ids = [f"m{i}" for i in range(1500)]
        batch_modify_labels(service, ids, add_label_ids=["A"], remove_label_ids=["R1", "R2"])
        calls = service.users().messages().batchModify.call_args_list
        bodies = [c.kwargs["body"] for c in calls if "body" in c.kwargs]
        assert [len(b["ids"]) for b in bodies] == [1000, 500]
        assert bodies[0]["addLabelIds"] == ["A"]
        assert bodies[0]["removeLabelIds"] == ["R1", "R2"]

    def test_empty_ids_makes_no_call(self):
        from claven.core.gmail import batch_modify_labels
        service = MagicMock()
        batch_modify_labels(service, [], remove_label_ids=["R"])
        assert not [c for c in service.users().messages().batchModify.call_args_list if "body" in c.kwargs]


# ---------------------------------------------------------------------------
# build_label_id_cache / migrate_legacy_labels
# ---------------------------------------------------------------------------

from googleapiclient.errors import HttpError

from claven.core.gmail import build_label_id_cache, migrate_legacy_labels
from claven.core.labels import LABEL_COLORS, label_body


def _http_error(status):
    resp = MagicMock(status=status, reason="err")
    return HttpError(resp, b"{}")


class TestMigrateLegacyLabels:
    def test_renames_old_label_in_place_with_style(self):
        service = mock_service()
        labels = [{"name": "c", "id": "P"}, {"name": "unknown-sender", "id": "Label_7"}]
        result = migrate_legacy_labels(service, labels)
        service.users().labels().patch.assert_called_once_with(
            userId="me", id="Label_7", body=label_body("c/u"))
        assert {"name": "c/u", "id": "Label_7"} in result
        assert not any(l["name"] == "unknown-sender" for l in result)

    def test_creates_parent_before_renaming(self):
        service = mock_service()
        service.users().labels().create().execute.return_value = {"name": "c", "id": "P"}
        result = migrate_legacy_labels(service, [{"name": "known-sender", "id": "Label_1"}])
        service.users().labels().create.assert_called_with(userId="me", body=label_body("c"))
        assert {"name": "c", "id": "P"} in result

    def test_leaves_old_label_alone_when_new_name_taken(self):
        service = mock_service()
        labels = [{"name": "c", "id": "P"}, {"name": "known-sender", "id": "L1"}, {"name": "c/k", "id": "L2"}]
        assert migrate_legacy_labels(service, labels) == labels
        service.users().labels().patch.assert_not_called()

    def test_no_calls_when_nothing_to_migrate(self):
        service = mock_service()
        labels = [{"name": "c/k", "id": "L2"}, {"name": "INBOX", "id": "INBOX"}]
        assert migrate_legacy_labels(service, labels) == labels
        service.users().labels().patch.assert_not_called()
        service.users().labels().create.assert_not_called()

    def test_retries_without_colour_if_gmail_rejects_it(self):
        service = mock_service()
        patch_call = service.users().labels().patch
        patch_call.return_value.execute.side_effect = [_http_error(400), {}]
        migrate_legacy_labels(service, [{"name": "c", "id": "P"}, {"name": "known-sender", "id": "L1"}])
        last_body = patch_call.call_args.kwargs["body"]
        assert last_body["name"] == "c/k" and "color" not in last_body


class TestRetiredLabels:
    def test_deletes_retired_label(self):
        service = mock_service()
        labels = [{"name": "c", "id": "P"}, {"name": "c/0h", "id": "L0"}, {"name": "c/1h", "id": "L1"}]
        result = migrate_legacy_labels(service, labels)
        service.users().labels().delete.assert_called_once_with(userId="me", id="L0")
        assert [l["name"] for l in result] == ["c", "c/1h"]

    def test_no_delete_when_nothing_retired(self):
        service = mock_service()
        migrate_legacy_labels(service, [{"name": "c/1h", "id": "L1"}])
        service.users().labels().delete.assert_not_called()


class TestBuildLabelIdCache:
    def test_uses_renamed_label_id(self):
        service = mock_service()
        service.users().labels().list().execute.return_value = {
            "labels": [{"name": "c", "id": "P"}, {"name": "known-sender", "id": "Label_1"}]
        }
        assert build_label_id_cache(service, ["c/k"]) == {"c/k": "Label_1"}
        service.users().labels().create.assert_not_called()

    def test_creates_missing_label_with_style(self):
        service = mock_service()
        service.users().labels().list().execute.return_value = {"labels": [{"name": "c", "id": "P"}]}
        service.users().labels().create().execute.return_value = {"name": "c/12h", "id": "L9"}
        assert build_label_id_cache(service, ["c/12h"]) == {"c/12h": "L9"}
        service.users().labels().create.assert_called_with(userId="me", body=label_body("c/12h"))
        assert label_body("c/12h")["color"] == LABEL_COLORS["c/12h"]

    def test_creates_parent_first(self):
        service = mock_service()
        service.users().labels().list().execute.return_value = {"labels": []}
        service.users().labels().create().execute.side_effect = [{"id": "P"}, {"id": "L2"}]
        assert build_label_id_cache(service, ["c/k"]) == {"c/k": "L2"}
        bodies = [c.kwargs["body"]["name"] for c in service.users().labels().create.call_args_list if c.kwargs]
        assert bodies[-2:] == ["c", "c/k"]


def test_create_label_uses_existing_id_when_another_worker_created_it():
    service = mock_service()
    service.users().labels().list().execute.side_effect = [
        {"labels": []},                                # first look: missing
        {"labels": [{"name": "c/k", "id": "L_race"}]},  # after the 409
    ]
    service.users().labels().create().execute.side_effect = [{"id": "P"}, _http_error(409)]
    assert build_label_id_cache(service, ["c/k"]) == {"c/k": "L_race"}


def test_ensure_label_exists_renames_legacy_sent_scanned():
    service = mock_service()
    service.users().labels().list().execute.return_value = {
        "labels": [{"name": "c", "id": "P"}, {"name": "claven/sent-scanned", "id": "Label_5"}]
    }
    assert ensure_label_exists(service, "c/s") == "Label_5"
    service.users().labels().patch.assert_called_once_with(userId="me", id="Label_5", body=label_body("c/s"))
