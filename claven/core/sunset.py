"""Sunset mode: unknown-sender mail stays in the inbox for a timer, then is
archived if it is still unread and unstarred.

Stateless: each sweep asks Gmail for unread, unstarred unknown-sender inbox
mail older than the cutoff, so no per-message state is stored and running a
sweep twice is harmless.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from claven.core.gmail import (
    _BATCH_LIMIT,
    batch_get_message_metadata,
    batch_modify_labels,
    batch_remove_labels,
    gmail_retry,
)
from claven.core.labels import LEGACY_NAMES

logger = logging.getLogger(__name__)

# label_only: label everything, never archive.
# gatekeeper: archive unknown-sender mail as soon as it is labeled.
# sunset: archive unknown-sender mail after a timer unless read or starred.
INBOX_MODES = ("label_only", "gatekeeper", "sunset")
SUNSET_HOURS_CHOICES = (4, 12, 24, 72)
DEFAULT_SUNSET_HOURS = 24

# Countdown labels on mail waiting in Sunset. A label names the largest step
# that still fits the time left, so "c/4h" means at least 4 hours remain.
COUNTDOWN_STEPS = ((72, "3d"), (24, "24h"), (12, "12h"), (4, "4h"), (1, "1h"))
COUNTDOWN_UNDER = "c/<1h"
COUNTDOWN_LABELS = tuple(f"c/{name}" for _, name in COUNTDOWN_STEPS) + (COUNTDOWN_UNDER,)
# Countdown names from before the c/ codes, still deleted when a user leaves Sunset.
_OLD_COUNTDOWN_LABELS = tuple(old for old, new in LEGACY_NAMES.items() if new in COUNTDOWN_LABELS)


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


def waiting_query(unknown_label: str) -> str:
    """Gmail search for unknown-sender mail still waiting in the inbox."""
    return f"in:inbox is:unread -is:starred label:{unknown_label}"


def _list_ids(service, **query) -> list[str]:
    """All message ids for a messages.list query (q= or labelIds=), across pages."""
    ids = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "maxResults": 500, **query}
        if page_token:
            kwargs["pageToken"] = page_token
        result = gmail_retry(lambda: service.users().messages().list(**kwargs).execute())
        ids.extend(m["id"] for m in result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return ids
        logger.debug("_list_ids: fetching next page")


def countdown_label(remaining: timedelta) -> str:
    """Countdown label for the time left, rounded down to a step."""
    for hours, name in COUNTDOWN_STEPS:
        if remaining >= timedelta(hours=hours):
            return f"c/{name}"
    logger.debug("countdown_label: %s left, under the smallest step", remaining)
    return COUNTDOWN_UNDER


def message_expiry(internal_date_ms: int, sunset_hours: int, mode_changed_at: datetime | None) -> datetime:
    """When a waiting message expires: arrival (or the switch to Sunset, if later) plus the timer."""
    start = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
    if mode_changed_at is not None and mode_changed_at > start:
        logger.debug("message_expiry: arrived before switching to Sunset, timer starts at the switch")
        start = mode_changed_at
    return start + timedelta(hours=sunset_hours)


def update_countdowns(service, unknown_label: str, now: datetime, sunset_hours: int,
                      mode_changed_at: datetime | None, label_ids: dict[str, str]) -> int:
    """Put the right countdown label on each waiting message; strip it from the rest.

    label_ids maps each name in COUNTDOWN_LABELS to its Gmail label id.
    Returns the number of waiting messages whose countdown label changed.
    """
    countdown_ids = set(label_ids.values())
    waiting = _list_ids(service, q=waiting_query(unknown_label))

    to_step = defaultdict(list)
    for i in range(0, len(waiting), _BATCH_LIMIT):
        metadata = batch_get_message_metadata(service, waiting[i:i + _BATCH_LIMIT], ["From"])
        for message_id, (_headers, labels, internal_date_ms) in metadata.items():
            if internal_date_ms is None:
                logger.debug("update_countdowns: %s has no date, skipping", message_id)
                continue
            remaining = message_expiry(internal_date_ms, sunset_hours, mode_changed_at) - now
            want = label_ids[countdown_label(remaining)]
            if set(labels) & countdown_ids != {want}:
                to_step[want].append(message_id)
            else:
                logger.debug("update_countdowns: %s already on the right step", message_id)

    changed = 0
    for want, ids in to_step.items():
        batch_modify_labels(service, ids, add_label_ids=[want], remove_label_ids=sorted(countdown_ids - {want}))
        changed += len(ids)

    # Read, starred, or archived mail no longer waits: drop its countdown label.
    waiting_set = set(waiting)
    stale = set()
    for label_id in countdown_ids:
        stale.update(m for m in _list_ids(service, labelIds=[label_id]) if m not in waiting_set)
    if stale:
        logger.debug("update_countdowns: clearing countdown from %d messages", len(stale))
        batch_modify_labels(service, sorted(stale), remove_label_ids=sorted(countdown_ids))
    else:
        logger.debug("update_countdowns: no stale countdown labels")

    logger.info("Sunset countdowns: %d waiting, %d moved, %d cleared", len(waiting), changed, len(stale))
    return changed


def clear_countdowns(service) -> int:
    """Delete Claven's countdown labels (which also removes them from every message).

    Used after a user leaves Sunset. Returns the number of labels deleted.
    """
    labels = gmail_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
    deleted = 0
    for label in labels:
        if label["name"] in COUNTDOWN_LABELS or label["name"] in _OLD_COUNTDOWN_LABELS:
            gmail_retry(lambda lid=label["id"]: service.users().labels().delete(userId="me", id=lid).execute())
            deleted += 1
        else:
            logger.debug("clear_countdowns: keeping label %s", label["name"])
    return deleted


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
