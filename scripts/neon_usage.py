"""
Check Claven's Neon project against the Free plan's monthly limits.

Usage:
    NEON_API_KEY=... NEON_ORG_ID=... NEON_PROJECT_ID=... python scripts/neon_usage.py

Claven runs on Neon's Free plan, which never bills: when a project uses up its
monthly compute or network transfer, Neon suspends it until the next period,
and Claven goes down. This check runs on a schedule (.github/workflows/neon-usage.yml)
so a month heading for a limit is caught while there is still time to cut usage.

It reads the project's usage counters, projects the period's average pace to
the period end, and prints a Markdown report. The exit code is 1 when anything
is at the alert level, so the workflow run fails and GitHub emails about it.
The Neon calls it makes don't wake the database.
"""

import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

API = "https://console.neon.tech/api/v2"

# Free plan limits per project (https://neon.com/docs/introduction/plans).
# Compute and transfer reset each period; storage is a standing limit, read
# from the project itself.
COMPUTE_LIMIT_CU_HOURS = 100
TRANSFER_LIMIT_BYTES = 5 * 10**9
MAX_CU = 0.25

WARN_AT = 0.6
ALERT_AT = 0.8
STORAGE_WARN_AT = 0.5

# A period's first hours say little about its pace; average over at least a day.
MIN_ELAPSED = timedelta(days=1)

LEVELS = ("ok", "warn", "alert")
ICONS = {"ok": "🟢", "warn": "🟡", "alert": "🔴"}


@dataclass
class Snapshot:
    plan: str
    org_id: str
    period_start: datetime
    period_end: datetime
    compute_cu_hours: float
    transfer_bytes: int
    storage_bytes: int
    storage_limit_bytes: int
    max_cus: list


@dataclass
class Finding:
    metric: str
    level: str
    message: str


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def snapshot(*, org: dict, project: dict, endpoints: list, branches: list) -> Snapshot:
    """The usage numbers that matter, from Neon's org, project, endpoint and branch responses.

    Compute is billed as awake time times compute size. Every compute is
    capped at MAX_CU, so the largest configured size gives an upper bound.
    """
    max_cus = [e["autoscaling_limit_max_cu"] for e in endpoints]
    cu = max(max_cus, default=MAX_CU)
    return Snapshot(
        plan=org["plan"],
        org_id=project["org_id"],
        period_start=_ts(project["consumption_period_start"]),
        period_end=_ts(project["consumption_period_end"]),
        compute_cu_hours=project["active_time_seconds"] * cu / 3600,
        transfer_bytes=project["data_transfer_bytes"],
        storage_bytes=max((b.get("logical_size") or 0 for b in branches), default=0),
        storage_limit_bytes=project["branch_logical_size_limit_bytes"],
        max_cus=max_cus,
    )


def projected(used: float, snap: Snapshot, now: datetime) -> float:
    """Usage at the period end if the period's average pace so far continues."""
    elapsed = max(now - snap.period_start, MIN_ELAPSED)
    return used * ((snap.period_end - snap.period_start) / elapsed)


def _monthly(metric: str, used: float, limit: float, unit: str, scale: float, snap: Snapshot, now: datetime) -> Finding:
    proj = projected(used, snap, now)
    message = (
        f"{used / scale:.1f} of {limit / scale:g} {unit} used; "
        f"on pace for {proj / scale:.1f} ({proj / limit:.0%}) by {snap.period_end:%b %d}"
    )
    if used >= ALERT_AT * limit or proj >= ALERT_AT * limit:
        logger.debug("_monthly: %s alert (used=%s projected=%s limit=%s)", metric, used, proj, limit)
        return Finding(metric, "alert", message)
    if proj >= WARN_AT * limit:
        logger.debug("_monthly: %s warn (projected=%s limit=%s)", metric, proj, limit)
        return Finding(metric, "warn", message)
    logger.debug("_monthly: %s ok (projected=%s limit=%s)", metric, proj, limit)
    return Finding(metric, "ok", message)


def assess(snap: Snapshot, *, expected_org: str, now: datetime) -> list:
    """One finding per check, each at level ok, warn or alert."""
    findings = [
        _monthly("compute", snap.compute_cu_hours, COMPUTE_LIMIT_CU_HOURS, "CU-hours", 1, snap, now),
        _monthly("transfer", snap.transfer_bytes, TRANSFER_LIMIT_BYTES, "GB", 10**9, snap, now),
    ]

    share = snap.storage_bytes / snap.storage_limit_bytes
    message = f"{snap.storage_bytes / 10**6:.0f} MB of {snap.storage_limit_bytes / 10**6:.0f} MB ({share:.0%})"
    if share >= ALERT_AT:
        logger.debug("assess: storage alert (share=%.2f)", share)
        findings.append(Finding("storage", "alert", message))
    elif share >= STORAGE_WARN_AT:
        logger.debug("assess: storage warn (share=%.2f)", share)
        findings.append(Finding("storage", "warn", message))
    else:
        logger.debug("assess: storage ok (share=%.2f)", share)
        findings.append(Finding("storage", "ok", message))

    if snap.plan != "free":
        logger.debug("assess: plan alert (plan=%s)", snap.plan)
        findings.append(Finding("plan", "alert", f"org is on the {snap.plan} plan, which bills; Claven belongs on Free"))
    else:
        logger.debug("assess: plan ok")
        findings.append(Finding("plan", "ok", "org is on the Free plan"))

    if snap.org_id != expected_org:
        logger.debug("assess: org alert (org_id=%s expected=%s)", snap.org_id, expected_org)
        findings.append(Finding("org", "alert", f"project is in {snap.org_id}, expected {expected_org}"))
    else:
        logger.debug("assess: org ok")
        findings.append(Finding("org", "ok", f"project is in {expected_org}"))

    if any(cu > MAX_CU for cu in snap.max_cus):
        logger.debug("assess: compute_size alert (max_cus=%s)", snap.max_cus)
        findings.append(Finding("compute_size", "alert", f"compute sizes {snap.max_cus} exceed the {MAX_CU} CU cap"))
    else:
        logger.debug("assess: compute_size ok (max_cus=%s)", snap.max_cus)
        findings.append(Finding("compute_size", "ok", f"computes capped at {MAX_CU} CU"))

    if len(snap.max_cus) > 1:
        logger.debug("assess: computes warn (count=%d)", len(snap.max_cus))
        findings.append(Finding("computes", "warn", f"{len(snap.max_cus)} computes share the monthly compute hours"))
    else:
        logger.debug("assess: computes ok (count=%d)", len(snap.max_cus))
        findings.append(Finding("computes", "ok", f"{len(snap.max_cus)} compute"))

    return findings


def worst(findings: list) -> str:
    return max((f.level for f in findings), key=LEVELS.index, default="ok")


def report(snap: Snapshot, findings: list, now: datetime) -> str:
    level = worst(findings)
    headline = {
        "ok": "Neon usage is within budget",
        "warn": "Neon usage is on pace to pass 60% of a Free plan limit",
        "alert": "Neon usage needs action before Claven is suspended",
    }[level]
    days_left = (snap.period_end - now) / timedelta(days=1)
    lines = [
        f"## {ICONS[level]} {headline}",
        "",
        f"{now:%Y-%m-%d %H:%M} UTC, {days_left:.1f} days left in the period ending {snap.period_end:%Y-%m-%d}.",
        "",
        "| | Check | Status |",
        "|---|---|---|",
    ]
    lines += [f"| {ICONS[f.level]} | {f.metric} | {f.message} |" for f in findings]
    return "\n".join(lines) + "\n"


def _get(path: str, key: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    key, org_id, project_id = (os.environ[k] for k in ("NEON_API_KEY", "NEON_ORG_ID", "NEON_PROJECT_ID"))
    snap = snapshot(
        org=_get(f"/organizations/{org_id}", key),
        project=_get(f"/projects/{project_id}", key)["project"],
        endpoints=_get(f"/projects/{project_id}/endpoints", key)["endpoints"],
        branches=_get(f"/projects/{project_id}/branches", key)["branches"],
    )
    now = datetime.now(timezone.utc)
    findings = assess(snap, expected_org=org_id, now=now)
    text = report(snap, findings, now)
    print(text)

    level = worst(findings)
    for var, content in (("GITHUB_STEP_SUMMARY", text), ("GITHUB_OUTPUT", f"level={level}\n")):
        if os.environ.get(var):
            logger.debug("main: writing %s", var)
            with open(os.environ[var], "a") as f:
                f.write(content)
        else:
            logger.debug("main: %s not set", var)
    return 1 if level == "alert" else 0


if __name__ == "__main__":
    sys.exit(main())
