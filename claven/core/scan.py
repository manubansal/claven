import json
import logging
import random

import claven.core.db as db
from claven.core.gmail import (
    batch_apply_labels,
    batch_get_message_headers,
    batch_get_message_metadata,
    batch_swap_labels,
    ensure_label_exists,
    get_message,
    get_profile,
    list_history,
    list_messages,
    load_scan_checkpoint,
    save_scan_checkpoint,
    _parse_addresses,
)
from claven.core.labels import SENT_SCANNED
from claven.core.process import process_message
import time

from claven.core.rules import matches_rule
from claven.core.sunset import unknown_group

logger = logging.getLogger(__name__)

running = True


def _interruptible_sleep(seconds, shutdown_event=None):
    """Sleep that wakes immediately if shutdown_event is set."""
    if shutdown_event is not None:
        logger.debug("_interruptible_sleep: event.wait(%.1f) (event_set=%s)", seconds, shutdown_event.is_set())
        shutdown_event.wait(seconds)
    else:
        logger.debug("_interruptible_sleep: time.sleep(%.1f) (no event)", seconds)
        time.sleep(seconds)

_BATCH_SIZE = 50

# Fetch more candidates than needed and randomly sample from them.
# This desynchronizes concurrent workers so they process different messages
# instead of always picking the same batch (Gmail returns newest-first by
# default, so without sampling two workers would grab identical batches).
_SAMPLE_POOL_MULTIPLIER = 5


def _sample_batch(candidates, batch_size):
    """Randomly sample a batch from a larger candidate pool.

    If the pool is larger than the batch size, pick randomly to reduce
    overlap between concurrent workers scanning the same mailbox.
    If the pool is smaller or equal, use all candidates.
    """
    if len(candidates) > batch_size:
        return random.sample(candidates, batch_size)
    return candidates


def _notify_progress(conn, user_id, event, **data):
    """Send a Postgres NOTIFY and write to event log.

    NOTIFY fires on the next commit, so call this before conn.commit().
    Payload is JSON with user_id, event type, and any extra data.
    """
    payload = json.dumps({"user_id": user_id, "event": event, **data})
    with conn.cursor() as cur:
        cur.execute("SELECT pg_notify('scan_progress', %s)", (payload,))

    # Build a human-readable message for the event log
    msg = _event_message(event, data)
    if msg:
        db.log_event(conn, user_id, event, msg)


def _event_message(event: str, data: dict) -> str | None:
    """Convert an event + data dict to a human-readable log message."""
    match event:
        case "sent_scan_progress":
            return f"Sent scan — {data.get('scanned', 0)} scanned, {data.get('senders', 0)} senders"
        case "inbox_scan_progress":
            return f"Label scan — {data.get('labeled', 0)} labeled"
        case "inbox_scan_complete":
            return f"Label scan complete — {data.get('labeled', 0)} labeled"
        case "archive_started":
            return f"Archive started — {data.get('total', 0)} messages"
        case "archive_progress":
            return f"Archive — {data.get('progress', 0)}/{data.get('total', 0)}"
        case "archive_complete":
            return f"Archive complete — {data.get('progress', 0)}/{data.get('total', 0)}"
        case "archive_cancelled":
            return f"Archive cancelled at {data.get('progress', 0)}/{data.get('total', 0)}"
        case "reset_sent_started":
            return f"Reset sent scan started — {data.get('total', 0)} messages"
        case "reset_sent_progress":
            return f"Reset sent scan — {data.get('progress', 0)}/{data.get('total', 0)}"
        case "reset_sent_complete":
            return f"Reset sent scan complete — {data.get('progress', 0)}/{data.get('total', 0)}"
        case "reset_sent_cancelled":
            return f"Reset sent scan cancelled at {data.get('progress', 0)}/{data.get('total', 0)}"
        case _:
            return None  # Skip noisy per-batch events without a clear message


SENT_SCANNED_LABEL = SENT_SCANNED


def build_known_senders(service, conn, user_id, should_continue=None, shutdown_event=None):
    """Build known senders list by scanning unlabeled sent messages.

    Queries Gmail for sent messages without the hidden 'c/s' label,
    extracts recipients, inserts into sent_recipients, and applies the label.
    Loops until no unlabeled sent messages remain.

    Progress is mailbox-based — multiple workers converge naturally since
    each queries remaining unlabeled messages and labels what it processes.

    conn is used for the initial setup only. Each batch opens a fresh
    connection to avoid Neon idle connection timeouts.

    shutdown_event: threading.Event that, when set, wakes sleeps immediately
    so the thread exits within one loop iteration instead of blocking.

    Returns the number of sent messages scanned in this run.
    """
    scanned_label_id = ensure_label_exists(service, SENT_SCANNED_LABEL)
    query = f"in:sent -label:{SENT_SCANNED_LABEL}"
    logger.info("Sent scan starting (query=%s)", query)

    total_scanned = 0
    batch_num = 0
    while True:
        if should_continue is not None and not should_continue():
            logger.info("Sent scan stopped by caller after %d scanned", total_scanned)
            logger.debug("build_known_senders: should_continue returned False")
            return total_scanned

        logger.debug("build_known_senders: querying for unlabeled sent messages (batch_num=%d)", batch_num)
        pool = list_messages(service, query=query, max_results=_BATCH_SIZE * _SAMPLE_POOL_MULTIPLIER)
        if not pool:
            logger.info("Sent scan complete: %d messages scanned, 0 remaining", total_scanned)
            logger.debug("build_known_senders: no more messages in pool")
            break

        batch_num += 1
        batch = _sample_batch(pool, _BATCH_SIZE)
        batch_ids = [m["id"] for m in batch]
        logger.info("Sent scan batch %d: %d unscanned messages", batch_num, len(batch_ids))

        # Batch fetch To/Cc/Bcc headers
        try:
            metadata = batch_get_message_metadata(service, batch_ids, ["To", "Cc", "Bcc"])
        except Exception as exc:
            logger.warning("Batch sent fetch failed: %s", exc)
            _interruptible_sleep(5, shutdown_event)
            continue

        # Extract recipients
        recipients = []
        scanned_ids = []
        for msg_id in batch_ids:
            result = metadata.get(msg_id)
            if not result:
                continue
            headers = result[0]
            for field in ("to", "cc", "bcc"):
                if value := headers.get(field):
                    for addr in _parse_addresses(value):
                        recipients.append(addr.lower())
            scanned_ids.append(msg_id)

        # DB first, then Gmail label. If DB commit fails, no label is
        # applied — next scan picks up the same messages. If label fails
        # after commit, recipients are saved and messages get re-processed
        # (idempotent via ON CONFLICT DO NOTHING).
        if recipients:
            logger.debug("build_known_senders: batch %d — %d recipients to insert", batch_num, len(recipients))
            with db.get_connection() as batch_conn:
                db.bulk_add_known_senders(batch_conn, user_id, recipients)
        else:
            logger.debug("build_known_senders: batch %d — no recipients extracted", batch_num)

        if scanned_ids:
            try:
                applied = batch_apply_labels(service, [(mid, scanned_label_id) for mid in scanned_ids])
                total_scanned += applied
                with db.get_connection() as batch_conn:
                    senders_count = db.count_known_senders(batch_conn, user_id)
                    _notify_progress(batch_conn, user_id, "sent_scan_progress",
                                     scanned=total_scanned, senders=senders_count)
                logger.info("Sent scan batch %d: scanned %d messages (%d total, %d senders)",
                            batch_num, applied, total_scanned, senders_count)
            except Exception as exc:
                logger.warning("Batch label apply failed: %s", exc)
                _interruptible_sleep(5, shutdown_event)

        _interruptible_sleep(1, shutdown_event)

    return total_scanned


def relabel_scan(service, user_id, label_configs, label_id_cache, should_continue=None, shutdown_event=None,
                 countdown_ids=None):
    """Relabel messages from newly discovered known senders.

    Finds senders with relabel_status='pending' in the DB, queries Gmail
    for their messages labeled unknown-sender, and swaps the label to
    known-sender atomically. Marks each sender as 'done' after processing.

    Returns the total number of messages relabeled.
    """
    with db.get_connection() as conn:
        pending_senders = db.get_pending_relabel_senders(conn, user_id)

    if not pending_senders:
        logger.debug("relabel_scan: no pending senders, returning 0")
        return 0

    logger.info("Relabel scan starting: %d pending senders", len(pending_senders))

    # Resolve label IDs
    unknown_label_id = None
    unknown_label_name = None
    known_label_id = None
    for lc in label_configs:
        if not known_label_id:
            known_label_id = label_id_cache.get(lc["id"])
        if unknown := lc.get("unknown_label"):
            if not unknown_label_id:
                unknown_label_id = label_id_cache.get(unknown)
                unknown_label_name = unknown

    if not unknown_label_id or not known_label_id:
        logger.warning("Relabel scan: missing label IDs (known=%s, unknown=%s)", known_label_id, unknown_label_id)
        return 0
    logger.debug("relabel_scan: resolved label IDs — known=%s, unknown=%s", known_label_id, unknown_label_id)

    total_relabeled = 0
    for sender in pending_senders:
        if should_continue is not None and not should_continue():
            logger.info("Relabel scan stopped by caller after %d relabeled", total_relabeled)
            logger.debug("relabel_scan: should_continue returned False")
            return total_relabeled

        # Find this sender's unknown-sender messages (in Sunset, waiting ones carry a countdown label)
        query = f"from:{sender} {unknown_group(unknown_label_name, list(countdown_ids or {}))}"
        msg_ids = []
        page_token = None
        while True:
            kwargs = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            msg_ids.extend(m["id"] for m in result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        if msg_ids:
            logger.debug("relabel_scan: sender=%s — %d messages to relabel", sender, len(msg_ids))
            # Swap labels in batches
            for i in range(0, len(msg_ids), _BATCH_SIZE):
                batch = msg_ids[i:i + _BATCH_SIZE]
                if countdown_ids:
                    swapped = batch_swap_labels(service, batch, unknown_label_id, known_label_id,
                                                also_remove=list(countdown_ids.values()))
                else:
                    logger.debug("relabel_scan: no countdown labels to drop")
                    swapped = batch_swap_labels(service, batch, unknown_label_id, known_label_id)
                total_relabeled += swapped
                _interruptible_sleep(1, shutdown_event)

            logger.info("Relabel scan: %s → %d messages relabeled", sender, len(msg_ids))
        else:
            logger.debug("relabel_scan: sender=%s — no unknown-sender messages found", sender)

        # Mark sender as done
        with db.get_connection() as conn:
            db.mark_relabel_done(conn, user_id, [sender])

    logger.info("Relabel scan complete: %d messages relabeled across %d senders", total_relabeled, len(pending_senders))
    return total_relabeled


def _unlabeled_query(label_configs, scope="inbox", countdown_labels=()):
    """Build a Gmail query for messages missing filter labels.

    scope='inbox': only inbox messages (default)
    scope='allmail': all messages, no inbox restriction
    countdown_labels: Sunset countdown labels in the mailbox; mail carrying one
        is unknown-sender mail already.
    """
    exclude = []
    for lc in label_configs:
        exclude.append(f"-label:{lc['id']}")
        if unknown := lc.get("unknown_label"):
            exclude.append(f"-label:{unknown}")
    exclude.extend(f"-label:{name}" for name in countdown_labels)
    prefix = "in:inbox " if scope != "allmail" else ""
    return prefix + " ".join(exclude)


def scan_inbox(service, conn, user_id, label_configs, label_id_cache, known_senders=None, should_continue=None, shutdown_event=None, scope="inbox", auto_archive_unknown=False,
               countdown_labels=()):
    """Label unlabeled messages using batch API calls.

    scope='inbox': label inbox messages as known-sender or unknown-sender
    scope='allmail': label all messages (same labels, no inbox restriction)

    Queries Gmail for messages that don't have the relevant label(s),
    processes them in batches, and repeats until none remain. Progress
    is based entirely on mailbox state — multiple workers naturally converge.

    conn is used for the initial setup only. Each batch opens a fresh
    connection to avoid Neon idle connection timeouts.

    shutdown_event: threading.Event that, when set, wakes sleeps immediately
    so the thread exits within one loop iteration instead of blocking.

    Returns the total number of messages labeled in this run.
    """
    query = _unlabeled_query(label_configs, scope=scope, countdown_labels=countdown_labels)
    logger.info("Inbox scan starting (query=%s, known_senders=%d)",
                query, len(known_senders or []))

    total_labeled = 0
    batch_num = 0
    while True:
        if should_continue is not None and not should_continue():
            logger.info("Inbox scan stopped by caller after %d labeled", total_labeled)
            logger.debug("scan_inbox: should_continue returned False")
            return total_labeled

        logger.debug("scan_inbox: querying unlabeled messages (batch_num=%d)", batch_num)
        unlabeled_pool = list_messages(service, query=query, max_results=_BATCH_SIZE * _SAMPLE_POOL_MULTIPLIER)

        # Touch last_fetched with a fresh connection
        with db.get_connection() as batch_conn:
            db.touch_last_fetched(batch_conn, user_id)

        if not unlabeled_pool:
            logger.info("Inbox scan complete: %d messages labeled, 0 remaining", total_labeled)
            logger.debug("scan_inbox: no more unlabeled messages")
            break

        batch_num += 1
        batch = _sample_batch(unlabeled_pool, _BATCH_SIZE)
        batch_ids = [m["id"] for m in batch]
        logger.info("Inbox scan batch %d: %d unlabeled messages to process", batch_num, len(batch_ids))

        # Batch fetch headers
        try:
            headers_map = batch_get_message_headers(service, batch_ids)
        except Exception as exc:
            logger.warning("Batch header fetch failed: %s", exc)
            _interruptible_sleep(5, shutdown_event)
            continue

        # Evaluate rules and collect label applications
        to_label = []
        to_archive = []  # unknown-sender message IDs to auto-archive
        newest_date_ms = None
        for msg_id in batch_ids:
            result = headers_map.get(msg_id)
            if not result:
                continue
            headers, existing_labels, internal_date_ms = result
            if not headers:
                continue
            for lc in label_configs:
                matched = any(matches_rule(headers, rule, known_senders) for rule in lc["rules"])
                apply_id = lc["id"] if matched else lc.get("unknown_label")
                if apply_id:
                    gmail_label_id = label_id_cache.get(apply_id)
                    if gmail_label_id and gmail_label_id not in existing_labels:
                        to_label.append((msg_id, gmail_label_id))
                        if not matched and auto_archive_unknown and "INBOX" in existing_labels:
                            to_archive.append(msg_id)
                        if internal_date_ms and (newest_date_ms is None or internal_date_ms > newest_date_ms):
                            newest_date_ms = internal_date_ms

        # Batch apply labels, then write progress with a fresh connection
        logger.debug("scan_inbox: batch %d — %d messages to label", batch_num, len(to_label))
        if to_label:
            try:
                applied = batch_apply_labels(service, to_label)
                total_labeled += applied
                if applied > 0:
                    with db.get_connection() as batch_conn:
                        db.touch_last_labeled(batch_conn, user_id)
                        if newest_date_ms:
                            db.update_newest_labeled(batch_conn, user_id, newest_date_ms)
                        _notify_progress(batch_conn, user_id, "inbox_scan_progress",
                                         labeled=total_labeled)
                logger.info("Inbox scan batch %d: labeled %d messages (%d total)", batch_num, applied, total_labeled)
            except Exception as exc:
                logger.warning("Batch label apply failed: %s", exc)
                _interruptible_sleep(5, shutdown_event)
        else:
            logger.info("Inbox scan batch %d: all %d already labeled by another worker", batch_num, len(batch_ids))

        # Auto-archive unknown-sender messages (remove from inbox)
        if to_archive:
            try:
                from claven.core.gmail import batch_remove_labels
                archived = batch_remove_labels(service, to_archive, ["INBOX"])
                logger.info("Inbox scan batch %d: auto-archived %d unknown-sender messages", batch_num, archived)
            except Exception as exc:
                logger.warning("Auto-archive failed: %s", exc)

        _interruptible_sleep(1, shutdown_event)

    # Update history_id to current point
    with db.get_connection() as batch_conn:
        profile = get_profile(service)
        db.set_history_id(batch_conn, user_id, int(profile["historyId"]))
        _notify_progress(batch_conn, user_id, "inbox_scan_complete", labeled=total_labeled)
    return total_labeled


def _recipients_from_messages(service, messages):
    """Extract all To/Cc/Bcc addresses from a list of message metadata stubs."""
    recipients = set()
    for msg_meta in messages:
        msg = get_message(service, msg_meta["id"], format="metadata",
                          metadata_headers=["To", "Cc", "Bcc"])
        for header in msg.get("payload", {}).get("headers", []):
            if header["name"].lower() in ("to", "cc", "bcc"):
                for addr in _parse_addresses(header["value"]):
                    recipients.add(addr.lower())
    return recipients


def initial_scan(service, data_dir, label_configs, label_id_cache, known_senders=None, max_messages=None):
    """Scan existing inbox messages and apply labels, skipping already-processed ones."""
    known_senders_count = len(known_senders) if known_senders else 0

    limit_str = str(max_messages) if max_messages else "all"
    logger.info("Running initial inbox scan (%s messages)...", limit_str)
    messages = list_messages(service, query="in:inbox", max_results=max_messages)

    processed_ids, checkpoint_senders_count = load_scan_checkpoint(data_dir)
    if processed_ids and known_senders_count > checkpoint_senders_count:
        logger.info(
            "Known senders list grew from %d to %d — reprocessing all messages to apply new labels",
            checkpoint_senders_count, known_senders_count,
        )
        processed_ids = set()

    pending = [m for m in messages if m["id"] not in processed_ids]
    skipped = len(messages) - len(pending)
    if skipped:
        logger.info("Skipping %d already-processed messages from checkpoint", skipped)

    total = len(pending)
    logger.info("Processing %d messages", total)
    if total == 0:
        return

    for i, msg in enumerate(pending, 1):
        if not running:
            logger.info("Scan interrupted at %d/%d messages, progress saved", i - 1, total)
            save_scan_checkpoint(processed_ids, data_dir, known_senders_count)
            return
        process_message(service, msg["id"], label_configs, label_id_cache, known_senders)
        processed_ids.add(msg["id"])
        if i % 10 == 0 or i == total:
            logger.info("Progress: %d/%d messages processed (%.0f%%)", i, total, 100 * i / total)
            save_scan_checkpoint(processed_ids, data_dir, known_senders_count)
