import json
import os
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from claven.core.labels import LEGACY_NAMES, label_body

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _path(data_dir, filename):
    return os.path.join(data_dir, filename)


CREDENTIALS_PATH = "credentials.json"


def get_service(data_dir):
    """Authenticate and return a Gmail API service instance."""
    token_path = _path(data_dir, "token.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"{CREDENTIALS_PATH} not found. "
                    "See README for one-time setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as token_file:
            token_file.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_profile(service):
    """Get the user's Gmail profile (includes historyId)."""
    return gmail_retry(lambda: service.users().getProfile(userId="me").execute())


def list_messages(service, query="in:inbox", max_results=100):
    """List message IDs matching a query, paginating as needed.

    Pass max_results=None to fetch all matching messages.
    """
    messages = []
    page_token = None
    while max_results is None or len(messages) < max_results:
        batch_size = 500 if max_results is None else min(max_results - len(messages), 500)
        results = gmail_retry(lambda pt=page_token, bs=batch_size: (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=bs, pageToken=pt)
            .execute()
        ))
        messages.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return messages


def list_history(service, start_history_id, label_id="INBOX"):
    """List history records since a given history ID."""
    records = []
    request = (
        service.users()
        .history()
        .list(userId="me", startHistoryId=start_history_id, labelId=label_id)
    )
    while request:
        response = gmail_retry(lambda r=request: r.execute())
        records.extend(response.get("history", []))
        request = (
            service.users()
            .history()
            .list_next(previous_request=request, previous_response=response)
        )
    return records


def get_message(service, message_id, format="metadata", metadata_headers=None):
    """Get a single message. Returns metadata by default."""
    kwargs = {"userId": "me", "id": message_id, "format": format}
    if metadata_headers:
        kwargs["metadataHeaders"] = metadata_headers
    return gmail_retry(lambda: service.users().messages().get(**kwargs).execute())


def get_message_headers(service, message_id):
    """Get From and To headers for a message.

    Subject is deliberately not fetched: no label rule uses it (data minimization).
    """
    msg = get_message(
        service,
        message_id,
        format="metadata",
        metadata_headers=["From", "To"],
    )
    headers = {}
    for header in msg.get("payload", {}).get("headers", []):
        name = header["name"].lower()
        if name in ("from", "to"):
            headers[name] = header["value"]
    return headers, msg.get("labelIds", [])


def _style_label(service, name: str, write) -> dict:
    """Create or patch a label with Claven's style; retry without colour if Gmail rejects it."""
    body = label_body(name)
    try:
        return gmail_retry(lambda: write(body).execute())
    except HttpError as exc:
        if "color" not in body or getattr(exc.resp, "status", None) != 400:
            logger.debug("_style_label: %s failed without a colour fallback: %s", name, exc)
            raise
        logger.warning("Gmail rejected colour for label %s, using default: %s", name, exc)
        plain = {k: v for k, v in body.items() if k != "color"}
        return gmail_retry(lambda: write(plain).execute())


def _create_label(service, name: str, labels: list[dict]) -> str:
    """Create a styled label (and its parent first, if missing). Appends to labels; returns the id."""
    names = {l["name"].lower() for l in labels}
    if "/" in name and (parent := name.rsplit("/", 1)[0]).lower() not in names:
        logger.debug("_create_label: creating parent %s for %s", parent, name)
        _create_label(service, parent, labels)
    else:
        logger.debug("_create_label: %s needs no new parent", name)
    try:
        created = _style_label(service, name, lambda b: service.users().labels().create(userId="me", body=b))
    except HttpError as exc:
        if getattr(exc.resp, "status", None) != 409:
            logger.debug("_create_label: creating %s failed: %s", name, exc)
            raise
        # Another worker created it first: use theirs.
        current = gmail_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
        label_id = next(l["id"] for l in current if l["name"].lower() == name.lower())
        logger.debug("_create_label: %s already created elsewhere (id: %s)", name, label_id)
        labels.append({"name": name, "id": label_id})
        return label_id
    logger.info("Created Gmail label: %s (id: %s)", name, created["id"])
    labels.append({"name": name, "id": created["id"]})
    return created["id"]


def migrate_legacy_labels(service, labels: list[dict]) -> list[dict]:
    """Rename labels from older Claven versions to their current names, in place.

    Renaming keeps the label on every message, so nothing has to be relabeled.
    A label is left alone if its new name is already taken. Returns the label
    list with the renames applied.
    """
    labels = list(labels)
    names = {l["name"] for l in labels}
    for label in list(labels):
        new_name = LEGACY_NAMES.get(label["name"])
        if new_name is None:
            logger.debug("migrate_legacy_labels: %s is current, keeping", label["name"])
            continue
        if new_name in names:
            logger.warning("Not renaming label %s: %s already exists", label["name"], new_name)
            continue
        parent = new_name.rsplit("/", 1)[0]
        if parent.lower() not in {n.lower() for n in names}:
            logger.debug("migrate_legacy_labels: creating parent %s", parent)
            _create_label(service, parent, labels)
            names.add(parent)
        else:
            logger.debug("migrate_legacy_labels: parent %s exists", parent)
        _style_label(service, new_name,
                     lambda b, lid=label["id"]: service.users().labels().patch(userId="me", id=lid, body=b))
        logger.info("Renamed Gmail label %s to %s (id: %s)", label["name"], new_name, label["id"])
        labels[labels.index(label)] = {**label, "name": new_name}
        names.discard(label["name"])
        names.add(new_name)
    return labels


def _list_labels_migrated(service) -> list[dict]:
    labels = gmail_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
    return migrate_legacy_labels(service, labels)


def build_label_id_cache(service, label_names: list[str]) -> dict[str, str]:
    """Return a {label_name: gmail_label_id} map, renaming old labels and creating missing ones."""
    labels = _list_labels_migrated(service)
    existing = {l["name"]: l["id"] for l in labels}
    cache = {}
    for name in label_names:
        if name in existing:
            cache[name] = existing[name]
        else:
            logger.debug("build_label_id_cache: %s missing, creating", name)
            cache[name] = _create_label(service, name, labels)
    return cache


def ensure_label_exists(service, label_name):
    """Create a label if it doesn't exist (renaming an old Claven label first). Returns the label ID."""
    labels = _list_labels_migrated(service)
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return label["id"]
    logger.debug("ensure_label_exists: %s missing, creating", label_name)
    return _create_label(service, label_name, labels)


def load_scan_checkpoint(data_dir):
    """Load the set of already-processed message IDs and known senders count from disk."""
    p = _path(data_dir, "scan_checkpoint.json")
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
            return set(data.get("processed_ids", [])), data.get("known_senders_count", 0)
    return set(), 0


def save_scan_checkpoint(processed_ids, data_dir, known_senders_count=0):
    """Persist the set of processed message IDs and known senders count to disk."""
    with open(_path(data_dir, "scan_checkpoint.json"), "w") as f:
        json.dump({
            "processed_ids": sorted(processed_ids),
            "known_senders_count": known_senders_count,
        }, f)


def _load_recipients_cache(data_dir):
    """Load cached recipients, history ID, and scan resume index from disk."""
    p = _path(data_dir, "sent_recipients_cache.json")
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
            return set(data.get("recipients", [])), data.get("history_id"), data.get("resume_index", 0)
    return set(), None, 0


def _save_recipients_cache(recipients, history_id, data_dir, resume_index=0):
    """Save recipients, history ID, and scan resume index to disk."""
    with open(_path(data_dir, "sent_recipients_cache.json"), "w") as f:
        json.dump({
            "recipients": sorted(recipients),
            "history_id": history_id,
            "resume_index": resume_index,
        }, f)


def _extract_recipients_from_messages(service, messages, should_continue=None):
    """Extract recipient addresses from a list of message metadata."""
    recipients = set()
    total = len(messages)
    log_every = max(1, total // 10)
    for i, msg_meta in enumerate(messages, 1):
        if should_continue is not None and not should_continue():
            logger.info("Recipient extraction interrupted at %d/%d", i - 1, total)
            return recipients, True  # (results, interrupted)
        msg = get_message(
            service, msg_meta["id"], format="metadata", metadata_headers=["To", "Cc", "Bcc"]
        )
        for header in msg.get("payload", {}).get("headers", []):
            if header["name"].lower() in ("to", "cc", "bcc"):
                for addr in _parse_addresses(header["value"]):
                    recipients.add(addr.lower())
        if total > 10 and (i % log_every == 0 or i == total):
            logger.info("Extracting recipients: %d/%d (%.0f%%)", i, total, 100 * i / total)
    return recipients, False  # (results, interrupted)


def list_sent_recipients(service, data_dir, should_continue=None):
    """Return a sorted set of all email addresses the user has ever sent to.

    Uses incremental caching: on first run, scans all sent messages and saves
    the results. On subsequent runs, only processes new messages since the last
    cached history ID.

    should_continue: optional callable; if it returns False the scan stops early
    and saves progress for the next run.
    """
    cached_recipients, cached_history_id, resume_index = _load_recipients_cache(data_dir)

    if cached_history_id:
        # Incremental update: only fetch new sent messages since last run
        logger.info("Loading cached recipients, checking for new sent messages...")
        try:
            records = list_history(service, cached_history_id, label_id="SENT")
            new_message_ids = set()
            for record in records:
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "SENT" in msg.get("labelIds", []):
                        new_message_ids.add(msg["id"])
            if new_message_ids:
                logger.info("Found %d new sent message(s)", len(new_message_ids))
                new_msgs = [{"id": mid} for mid in new_message_ids]
                new_recipients, _ = _extract_recipients_from_messages(service, new_msgs, should_continue)
                cached_recipients.update(new_recipients)
            else:
                logger.info("No new sent messages since last run")
        except Exception as e:
            if "404" in str(e):
                logger.warning("History ID expired, doing full scan")
                cached_recipients = set()
                cached_history_id = None
            else:
                raise

    if not cached_history_id:
        # Full scan: collect all sent message IDs first so we know the total,
        # then process with an accurate global progress percentage.
        logger.info("Fetching list of all sent messages...")
        all_messages = list_messages(service, query="in:sent", max_results=None)
        total = len(all_messages)
        logger.info("Found %d sent messages to process", total)

        if resume_index:
            logger.info("Resuming from message %d (skipping %d already processed)",
                        resume_index + 1, resume_index)

        for i, msg_meta in enumerate(all_messages[resume_index:], resume_index + 1):
            if should_continue is not None and not should_continue():
                logger.info("Sent recipients scan interrupted at %d/%d, progress saved", i - 1, total)
                _save_recipients_cache(cached_recipients, None, data_dir, resume_index=i - 1)
                return sorted(cached_recipients)
            msg = get_message(service, msg_meta["id"], format="metadata",
                              metadata_headers=["To", "Cc", "Bcc"])
            for header in msg.get("payload", {}).get("headers", []):
                if header["name"].lower() in ("to", "cc", "bcc"):
                    for addr in _parse_addresses(header["value"]):
                        cached_recipients.add(addr.lower())
            if i % 10 == 0 or i == total:
                logger.info("Processing sent messages: %d/%d (%.0f%%)", i, total, 100 * i / total)
                _save_recipients_cache(cached_recipients, None, data_dir, resume_index=i)

        logger.info("Sent messages scan complete: %d total", total)

    # Save final cache with current history ID
    profile = get_profile(service)
    _save_recipients_cache(cached_recipients, profile["historyId"], data_dir)

    return sorted(cached_recipients)


def _parse_addresses(header_value):
    """Extract email addresses from a header value like 'Name <email>, ...'."""
    import re
    return re.findall(r"[\w.+-]+@[\w.-]+\.\w+", header_value)


def apply_label(service, message_id, label_id):
    """Apply a label to a message."""
    gmail_retry(lambda: service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute())
    logger.debug("Applied label %s to message %s", label_id, message_id)


def gmail_retry(fn, max_retries=3):
    """Call fn() with retry on 429/5xx errors. Returns the result."""
    import time as _time
    for attempt in range(max_retries):
        try:
            result = fn()
            if attempt > 0:
                logger.debug("gmail_retry: succeeded on attempt %d", attempt + 1)
            return result
        except Exception as exc:
            if attempt < max_retries - 1 and ("429" in str(exc) or "5" == str(getattr(getattr(exc, 'resp', None), 'status', '0'))[0:1]):
                delay = 5 * (attempt + 1)
                logger.warning("Gmail API call failed (attempt %d, retry in %ds): %s", attempt + 1, delay, exc)
                _time.sleep(delay)
            else:
                logger.debug("gmail_retry: giving up after attempt %d: %s", attempt + 1, exc)
                raise


_BATCH_LIMIT = 50


def _run_batch_with_retry(service, requests, callback, max_retries=3):
    """Execute a batch of Gmail API requests with retry on failure.

    requests: list of (request_object, request_id) tuples
    callback: fn(request_id, response, exception) called per request
    max_retries: total attempts before giving up

    Returns list of request_ids that permanently failed.
    """
    import time as _time
    pending = list(requests)

    for attempt in range(max_retries + 1):
        if not pending:
            logger.debug("_run_batch_with_retry: no pending requests, done")
            break
        if attempt > 0:
            delay = min(5 * (2 ** (attempt - 1)), 30)
            logger.info("Retrying %d failed batch requests (attempt %d, backoff %ds)", len(pending), attempt + 1, delay)
            _time.sleep(delay)

        failed = []

        def _wrap_callback(rid, resp, exc, _failed=failed):
            if exc:
                _failed.append(rid)
            callback(rid, resp, exc)

        batch = service.new_batch_http_request(callback=_wrap_callback)
        for req, rid in pending:
            batch.add(req, request_id=rid)

        try:
            batch.execute()
            logger.debug("_run_batch_with_retry: attempt %d — %d succeeded, %d failed",
                         attempt + 1, len(pending) - len(failed), len(failed))
        except Exception as exc:
            logger.warning("Batch execute failed (attempt %d): %s", attempt + 1, exc)
            failed = [rid for _, rid in pending]

        pending = [(req, rid) for req, rid in pending if rid in failed]

    remaining = [rid for _, rid in pending]
    if remaining:
        logger.debug("_run_batch_with_retry: %d requests permanently failed", len(remaining))
    return remaining


def batch_get_message_metadata(service, message_ids, metadata_headers=None, max_retries=3):
    """Fetch metadata for up to _BATCH_LIMIT messages in a single batch request.

    Returns {message_id: (headers_dict, label_ids_list, internal_date_ms)}
    for successful fetches. Failed fetches are omitted from the result.
    """
    fetch_headers = metadata_headers or ["From", "To"]
    header_names = {h.lower() for h in fetch_headers}
    results = {}

    def _callback(request_id, response, exception):
        if not exception:
            headers = {}
            for header in response.get("payload", {}).get("headers", []):
                name = header["name"].lower()
                if name in header_names:
                    headers[name] = header["value"]
            internal_date = response.get("internalDate")
            results[request_id] = (headers, response.get("labelIds", []), int(internal_date) if internal_date else None)

    requests = [
        (service.users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=fetch_headers), mid)
        for mid in message_ids[:_BATCH_LIMIT]
    ]
    _run_batch_with_retry(service, requests, _callback, max_retries)
    return results


def batch_get_message_headers(service, message_ids):
    """Fetch From/To headers for up to 100 messages. Convenience wrapper."""
    return batch_get_message_metadata(service, message_ids, ["From", "To"])


def batch_apply_labels(service, message_label_pairs, max_retries=3):
    """Apply labels to up to _BATCH_LIMIT messages in a single batch request.

    Returns the number of successful applications.
    """
    applied = 0
    pairs_by_id = {msg_id: label_id for msg_id, label_id in message_label_pairs[:_BATCH_LIMIT]}

    def _callback(request_id, response, exception):
        nonlocal applied
        if not exception:
            applied += 1

    requests = [
        (service.users().messages().modify(userId="me", id=mid, body={"addLabelIds": [pairs_by_id[mid]]}), mid)
        for mid in pairs_by_id
    ]
    _run_batch_with_retry(service, requests, _callback, max_retries)
    return applied


def batch_remove_labels(service, message_ids, label_ids_to_remove, max_retries=3):
    """Remove labels from up to _BATCH_LIMIT messages in a single batch request.

    Returns the number of successful modifications.
    """
    modified = 0

    def _callback(request_id, response, exception):
        nonlocal modified
        if not exception:
            modified += 1

    requests = [
        (service.users().messages().modify(userId="me", id=mid, body={"removeLabelIds": label_ids_to_remove}), mid)
        for mid in message_ids[:_BATCH_LIMIT]
    ]
    _run_batch_with_retry(service, requests, _callback, max_retries)
    return modified


def batch_modify_labels(service, message_ids, add_label_ids=(), remove_label_ids=()):
    """Add and/or remove labels on many messages with messages.batchModify.

    batchModify takes up to 1,000 ids per call; removing a label a message
    doesn't have is a no-op.
    """
    for i in range(0, len(message_ids), 1000):
        body = {
            "ids": message_ids[i:i + 1000],
            "addLabelIds": list(add_label_ids),
            "removeLabelIds": list(remove_label_ids),
        }
        gmail_retry(lambda b=body: service.users().messages().batchModify(userId="me", body=b).execute())


def batch_swap_labels(service, message_ids, remove_label_id, add_label_id, max_retries=3):
    """Atomically swap labels on up to _BATCH_LIMIT messages.

    Each message gets removeLabelIds + addLabelIds in a single modify call,
    so there's no window where the message has neither label.

    Returns the number of successful swaps.
    """
    swapped = 0

    def _callback(request_id, response, exception):
        nonlocal swapped
        if not exception:
            swapped += 1

    requests = [
        (service.users().messages().modify(
            userId="me", id=mid,
            body={"removeLabelIds": [remove_label_id], "addLabelIds": [add_label_id]},
        ), mid)
        for mid in message_ids[:_BATCH_LIMIT]
    ]
    _run_batch_with_retry(service, requests, _callback, max_retries)
    return swapped
