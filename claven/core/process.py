import logging

from claven.core.gmail import get_message_headers, apply_label, gmail_retry, list_history
from claven.core.rules import matches_rule

logger = logging.getLogger(__name__)


def process_message(service, message_id, label_configs, label_id_cache, known_senders=None, auto_archive_unknown=False):
    """Evaluate each label config against a message.

    For each label config:
    - If any rule matches, apply that label's Gmail label (keyed by id).
    - If no rule matches and the config has an unknown_label, apply that instead.

    This ensures every processed message lands in exactly one state per label
    config: matched, unmatched, or (if no unknown_label configured) unchanged.

    Returns True if at least one label was newly applied, False otherwise.
    """
    try:
        headers, existing_labels = get_message_headers(service, message_id)
    except Exception as exc:
        if "404" in str(exc) or "notFound" in str(exc):
            logger.debug("process_message: message %s not found (deleted?), skipping", message_id)
            return False
        raise
    if not headers:
        logger.debug("process_message: no headers for %s, skipping", message_id)
        return False

    applied = False
    for label_config in label_configs:
        matched = any(
            matches_rule(headers, rule, known_senders)
            for rule in label_config["rules"]
        )
        apply_id = label_config["id"] if matched else label_config.get("unknown_label")
        if apply_id:
            gmail_label_id = label_id_cache.get(apply_id)
            if gmail_label_id and gmail_label_id not in existing_labels:
                apply_label(service, message_id, gmail_label_id)
                logger.info("Labeled message %s as '%s'", message_id, apply_id)
                applied = True
                # Auto-archive: remove from inbox if labeled unknown-sender
                if not matched and auto_archive_unknown and "INBOX" in existing_labels:
                    gmail_retry(lambda: service.users().messages().modify(
                        userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
                    ).execute())
                    logger.info("Auto-archived unknown-sender message %s", message_id)
            else:
                logger.debug("process_message: %s already has label %s or label not in cache", message_id, apply_id)
    return applied


def poll_new_messages(service, history_id, label_configs, label_id_cache, known_senders=None, auto_archive_unknown=False):
    """Check for new messages since the last history ID.

    Returns the number of messages processed, or None if the history ID had
    expired (404) and the caller should fall back to a full inbox scan.
    """
    try:
        records = list_history(service, history_id)
    except Exception as e:
        if "404" in str(e):
            logger.warning("History ID expired, falling back to inbox scan")
            return None
        logger.debug("poll_new_messages: list_history raised non-404: %s", e)
        raise

    message_ids = set()
    for record in records:
        for added in record.get("messagesAdded", []):
            msg = added["message"]
            if "INBOX" in msg.get("labelIds", []):
                message_ids.add(msg["id"])

    logger.debug("poll_new_messages: %d history records, %d new inbox messages", len(records), len(message_ids))
    if message_ids:
        logger.info("Processing %d new message(s)", len(message_ids))
        for message_id in message_ids:
            process_message(service, message_id, label_configs, label_id_cache, known_senders, auto_archive_unknown=auto_archive_unknown)

    return len(message_ids)
