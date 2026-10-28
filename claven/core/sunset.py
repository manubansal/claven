"""Sunset mode: unknown-sender mail stays in the inbox for a timer, then is
archived if it is still unread and unstarred.

While mail waits, a countdown label (c/24h, ...) takes the place of the
unknown label: a countdown already means "unknown sender". Once the mail
stops waiting (read, starred, archived, or the user leaves Sunset) it goes
back to the unknown label.

Stateless: each sweep asks Gmail for unread, unstarred unknown-sender inbox
mail older than the cutoff, so no per-message state is stored and running a
sweep twice is harmless.
"""

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from claven.core.gmail import (
    _BATCH_LIMIT,
    batch_get_message_metadata,
    batch_modify_labels,
    batch_remove_labels,
    gmail_retry,
)
from claven.core.labels import COUNTDOWN_LABELS, COUNTDOWN_MAX_HOURS, LEGACY_NAMES, RETIRED_NAMES

logger = logging.getLogger(__name__)

# label_only: label everything, never archive.
# gatekeeper: archive unknown-sender mail as soon as it is labeled.
# sunset: archive unknown-sender mail after a timer unless read or starred.
INBOX_MODES = ("label_only", "gatekeeper", "sunset")
SUNSET_HOURS_CHOICES = (4, 12, 24, 72)
DEFAULT_SUNSET_HOURS = 24

# Countdown labels on mail waiting in Sunset, one per hour: "c/4h" means the
# mail is gone within 4 hours. Fresh mail shows the full timer the user picked
# for its first hour, so a step down means the mail has sat through an hour.
# Older countdown names, still deleted when a user leaves Sunset.
_OLD_COUNTDOWN_LABELS = (
    tuple(old for old, new in LEGACY_NAMES.items() if new in COUNTDOWN_LABELS) + tuple(RETIRED_NAMES)
)


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


def unknown_group(unknown_label: str, countdown_labels) -> str:
    """Gmail search term for unknown-sender mail: the unknown label or any countdown label.

    Pass only countdown labels that exist, so the search never names a missing label.
    """
    names = [unknown_label, *countdown_labels]
    if len(names) == 1:
        logger.debug("unknown_group: no countdown labels")
        return f"label:{unknown_label}"
    return "{" + " ".join(f"label:{name}" for name in names) + "}"


def sunset_query(unknown_label: str, cutoff: datetime, countdown_labels) -> str:
    """Gmail search for unknown-sender inbox mail that has expired unread."""
    return f"{waiting_query(unknown_label, countdown_labels)} before:{int(cutoff.timestamp())}"


def waiting_query(unknown_label: str, countdown_labels) -> str:
    """Gmail search for unknown-sender mail still waiting in the inbox."""
    return f"in:inbox is:unread -is:starred {unknown_group(unknown_label, countdown_labels)}"


def stale_query(countdown_labels) -> str:
    """Gmail search for countdown-labeled mail that stopped waiting: archived, read, or starred."""
    group = "{" + " ".join(f"label:{name}" for name in countdown_labels) + "}"
    return f"{group} {{-in:inbox is:read is:starred}}"


def countdown_labels_for(sunset_hours: int) -> tuple[str, ...]:
    """The countdown labels a timer uses, longest first."""
    return COUNTDOWN_LABELS[COUNTDOWN_MAX_HOURS - sunset_hours:]


def existing_countdown_ids(service) -> dict[str, str]:
    """{name: id} for the countdown labels in the mailbox, without creating any."""
    labels = gmail_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
    return {label["name"]: label["id"] for label in labels if label["name"] in COUNTDOWN_LABELS}


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


def _is_waiting(labels) -> bool:
    """Whether a message is unread and unstarred in the inbox."""
    return {"INBOX", "UNREAD"} <= set(labels) and "STARRED" not in labels


def countdown_label(remaining: timedelta, max_hours: int = COUNTDOWN_MAX_HOURS) -> str:
    """Countdown label for the time left, rounded up to the hour ("gone within"), at most max_hours."""
    hours = math.ceil(remaining.total_seconds() / 3600)
    if hours < 1:
        logger.debug("countdown_label: %s left, overdue: last hour", remaining)
        return "c/1h"
    if hours > max_hours:
        logger.debug("countdown_label: %s left, beyond the %dh timer", remaining, max_hours)
        return f"c/{max_hours}h"
    return f"c/{hours}h"


def message_expiry(internal_date_ms: int, sunset_hours: int, mode_changed_at: datetime | None) -> datetime:
    """When a waiting message expires: arrival (or the switch to Sunset, if later) plus the timer."""
    start = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
    if mode_changed_at is not None and mode_changed_at > start:
        logger.debug("message_expiry: arrived before switching to Sunset, timer starts at the switch")
        start = mode_changed_at
    return start + timedelta(hours=sunset_hours)


def arrival_countdown(internal_date_ms: int, sunset_hours: int, mode_changed_at: datetime | None,
                      now: datetime | None = None) -> str:
    """Countdown label for a message being labeled right now."""
    now = now or datetime.now(timezone.utc)
    return countdown_label(message_expiry(internal_date_ms, sunset_hours, mode_changed_at) - now, sunset_hours)


def update_countdowns(service, unknown_label: str, unknown_label_id: str, now: datetime, sunset_hours: int,
                      mode_changed_at: datetime | None, label_ids: dict[str, str]) -> int:
    """Put the right countdown label, in place of the unknown label, on each waiting
    message; put the unknown label back on mail that stopped waiting.

    label_ids maps each countdown label in the mailbox to its Gmail label id,
    including every hour of the user's timer. Searches can return other mail in
    the same thread, so only messages carrying the unknown or a countdown label
    are changed. Returns the number of waiting messages whose labels changed.
    """
    names = [name for name in COUNTDOWN_LABELS if name in label_ids]
    countdown_ids = set(label_ids.values())
    marker_ids = countdown_ids | {unknown_label_id}
    waiting = _list_ids(service, q=waiting_query(unknown_label, names))

    to_step = defaultdict(list)
    for i in range(0, len(waiting), _BATCH_LIMIT):
        metadata = batch_get_message_metadata(service, waiting[i:i + _BATCH_LIMIT], ["From"])
        for message_id, (_headers, labels, internal_date_ms) in metadata.items():
            if internal_date_ms is None:
                logger.debug("update_countdowns: %s has no date, skipping", message_id)
                continue
            if not set(labels) & marker_ids or not _is_waiting(labels):
                logger.debug("update_countdowns: %s matched by its thread only, skipping", message_id)
                continue
            remaining = message_expiry(internal_date_ms, sunset_hours, mode_changed_at) - now
            want = label_ids[countdown_label(remaining, sunset_hours)]
            if set(labels) & marker_ids != {want}:
                to_step[want].append(message_id)
            else:
                logger.debug("update_countdowns: %s already on the right step", message_id)

    changed = 0
    for want, ids in to_step.items():
        batch_modify_labels(service, ids, add_label_ids=[want], remove_label_ids=sorted(marker_ids - {want}))
        changed += len(ids)

    # Read, starred, or archived mail no longer waits: back to the unknown label.
    candidates = _list_ids(service, q=stale_query(names)) if names else []
    stale = set()
    for i in range(0, len(candidates), _BATCH_LIMIT):
        metadata = batch_get_message_metadata(service, candidates[i:i + _BATCH_LIMIT], ["From"])
        stale.update(m for m, (_headers, labels, _date) in metadata.items()
                     if set(labels) & countdown_ids and not _is_waiting(labels))
    if stale:
        logger.debug("update_countdowns: clearing countdown from %d messages", len(stale))
        batch_modify_labels(service, sorted(stale), add_label_ids=[unknown_label_id],
                            remove_label_ids=sorted(countdown_ids))
    else:
        logger.debug("update_countdowns: no stale countdown labels")

    logger.info("Sunset countdowns: %d waiting, %d moved, %d cleared", len(waiting), changed, len(stale))
    return changed


def clear_countdowns(service, unknown_label_id: str) -> int:
    """Delete Claven's countdown labels, putting the unknown label back on their mail first.

    Used after a user leaves Sunset. Returns the number of labels deleted.
    """
    labels = gmail_retry(lambda: service.users().labels().list(userId="me").execute()).get("labels", [])
    deleted = 0
    for label in labels:
        if label["name"] in COUNTDOWN_LABELS or label["name"] in _OLD_COUNTDOWN_LABELS:
            message_ids = _list_ids(service, labelIds=[label["id"]])
            if message_ids:
                logger.debug("clear_countdowns: %d messages on %s go back to unknown", len(message_ids), label["name"])
                batch_modify_labels(service, message_ids, add_label_ids=[unknown_label_id])
            else:
                logger.debug("clear_countdowns: %s is on no messages", label["name"])
            gmail_retry(lambda lid=label["id"]: service.users().labels().delete(userId="me", id=lid).execute())
            deleted += 1
        else:
            logger.debug("clear_countdowns: keeping label %s", label["name"])
    return deleted


def archive_expired_unknown(service, unknown_label: str, cutoff: datetime, countdown_labels) -> int:
    """Archive (remove INBOX from) expired unknown-sender mail. Returns the count archived.

    countdown_labels: the countdown labels in the mailbox (waiting mail carries one instead of c/u).
    """
    query = sunset_query(unknown_label, cutoff, countdown_labels)
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
