"""Cloud Scheduler jobs (scripts/setup-scheduler.sh)."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-scheduler.sh"


def _schedules() -> dict[str, str]:
    return dict(re.findall(r'^create_or_update_job "([^"]+)" "([^"]+)"', SCRIPT.read_text(), re.M))


def test_poll_runs_every_20_minutes():
    """The poll labels new mail (Gmail pushes are only acknowledged). Each run
    wakes the Neon database for at least 5 minutes, and the Free plan allows
    about 13 awake hours a day: 72 runs keep it near half of that."""
    assert _schedules()["claven-poll"] == "*/20 * * * *"


def test_sunset_runs_on_the_hour_with_the_poll():
    """The hourly Sunset sweep lands on a poll run, so the two share one wake."""
    assert _schedules()["claven-sunset"] == "0 * * * *"
