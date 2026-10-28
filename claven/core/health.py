"""Scan health codes — deterministic error/warning/info codes.

Each code is a severity letter + 6-char hash of the label string.
The hash is computed at import time from the label, so the same
error always produces the same code. Greppable in code and logs.

Usage:
    from claven.core.health import HEALTH_CODES, compute_scan_health

    code = compute_scan_health(inbox_scan_status, last_fetched_at)
    # Returns e.g. {"code": "E-a3f2c1", "label": "db.connection_lost", "severity": "error"}
"""

import hashlib
from datetime import datetime, timezone, timedelta


def _make_code(severity_letter: str, label: str) -> str:
    """Generate a deterministic code: severity letter + first 6 hex of sha256(label)."""
    h = hashlib.sha256(label.encode()).hexdigest()[:6]
    return f"{severity_letter}{h}"


# ── Code registry ────────────────────────────────────────────────────────────
# Each entry: (severity_letter, label) → generates the code at import time.

_REGISTRY = [
    ("I", "info.ok"),
    ("I", "info.waiting"),
    ("S", "success.complete"),
    ("W", "warning.scan.cancelled"),
    ("W", "warning.scan.stalled"),
    ("E", "error.scan.stalled"),
    ("E", "error.db.connection_lost"),
    ("E", "error.db.deadlock"),
    ("E", "error.gmail.api"),
    ("E", "error.gmail.rate_limited"),
    ("E", "error.gmail.auth_expired"),
    ("E", "error.gmail.quota_exhausted"),
    ("E", "error.unknown"),
]

# Built at import time: label → {code, label, severity}
HEALTH_CODES: dict[str, dict] = {}
for _sev, _label in _REGISTRY:
    HEALTH_CODES[_label] = {
        "code": _make_code(_sev, _label),
        "label": _label,
        "severity": {"I": "info", "S": "success", "W": "warning", "E": "error"}[_sev],
    }

# Convenience constants
OK = HEALTH_CODES["info.ok"]
WAITING = HEALTH_CODES["info.waiting"]
COMPLETE = HEALTH_CODES["success.complete"]
CANCELLED = HEALTH_CODES["warning.scan.cancelled"]
STALLED = HEALTH_CODES["warning.scan.stalled"]
ERROR_STALLED = HEALTH_CODES["error.scan.stalled"]
ERROR_DB = HEALTH_CODES["error.db.connection_lost"]
ERROR_GMAIL = HEALTH_CODES["error.gmail.api"]
ERROR_GMAIL_RATE_LIMITED = HEALTH_CODES["error.gmail.rate_limited"]
ERROR_GMAIL_AUTH_EXPIRED = HEALTH_CODES["error.gmail.auth_expired"]
ERROR_GMAIL_QUOTA_EXHAUSTED = HEALTH_CODES["error.gmail.quota_exhausted"]
ERROR_UNKNOWN = HEALTH_CODES["error.unknown"]

_STALE_THRESHOLD = timedelta(minutes=2)


def compute_scan_health(
    inbox_scan_status: str | None,
    last_fetched_at: datetime | None,
) -> dict | None:
    """Compute the current scan health code.

    Returns a health code dict or None if no scan is active.
    """
    if inbox_scan_status is None:
        return None

    if inbox_scan_status == "complete":
        return COMPLETE

    if inbox_scan_status == "in_progress":
        if last_fetched_at:
            age = datetime.now(timezone.utc) - last_fetched_at
            if age > _STALE_THRESHOLD:
                return STALLED
        else:
            # in_progress but never fetched — just started, give it time
            pass
        return OK

    # Status is an error code stored by the scan runner
    if inbox_scan_status.startswith("error"):
        # Map stored error labels back to health codes
        for label, entry in HEALTH_CODES.items():
            if label == inbox_scan_status:
                return entry
        # Fallback for generic "error" status (legacy)
        return ERROR_UNKNOWN

    return None
