"""Sunset mode: unknown-sender mail stays in the inbox for a timer, then is
archived if it is still unread and unstarred.

Stateless: each sweep asks Gmail for unread, unstarred unknown-sender inbox
mail older than the cutoff, so no per-message state is stored and running a
sweep twice is harmless.
"""

import logging
from datetime import datetime, timedelta

from claven.core.gmail import _BATCH_LIMIT, batch_remove_labels, gmail_retry

logger = logging.getLogger(__name__)

# label_only: label everything, never archive.
# gatekeeper: archive unknown-sender mail as soon as it is labeled.
# sunset: archive unknown-sender mail after a timer unless read or starred.
INBOX_MODES = ("label_only", "gatekeeper", "sunset")
SUNSET_HOURS_CHOICES = (4, 12, 24, 72)
DEFAULT_SUNSET_HOURS = 24


def sunset_cutoff(now: datetime, sunset_hours: int, mode_changed_at: datetime | None) -> datetime | None:
    """Return the arrival time before which unread unknown-sender mail expires.

    The timer for mail already in the inbox starts when Sunset was turned on,
    so nothing expires until a full timer has passed since the mode change.
    Returns None when nothing can have expired yet.
    """
    timer = timedelta(hours=sunset_hours)
    if mode_changed_at is not None and now - mode_changed_at < timer:
        logger.debug("sunset_cutoff: timer not yet run since mode change at %s", mode_changed_at)
        return None
    logger.debug("sunset_cutoff: cutoff %s (timer %dh)", now - timer, sunset_hours)
    return now - timer


def sunset_query(unknown_label: str, cutoff: datetime) -> str:
    """Gmail search for unknown-sender inbox mail that has expired unread."""
    return f"in:inbox is:unread -is:starred label:{unknown_label} before:{int(cutoff.timestamp())}"


def archive_expired_unknown(service, unknown_label: str, cutoff: datetime) -> int:
    """Archive (remove INBOX from) expired unknown-sender mail. Returns the count archived."""
    query = sunset_query(unknown_label, cutoff)
    message_ids = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = gmail_retry(lambda: service.users().messages().list(**kwargs).execute())
        message_ids.extend(m["id"] for m in result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            logger.debug("archive_expired_unknown: listed %d expired messages", len(message_ids))
            break
        logger.debug("archive_expired_unknown: fetching next page")

    if not message_ids:
        logger.debug("archive_expired_unknown: nothing expired")
        return 0

    archived = 0
    for i in range(0, len(message_ids), _BATCH_LIMIT):
        archived += batch_remove_labels(service, message_ids[i:i + _BATCH_LIMIT], ["INBOX"])
    logger.info("Sunset archived %d of %d expired unknown-sender messages", archived, len(message_ids))
    return archived
