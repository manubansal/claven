"""Cloud Scheduler jobs (scripts/setup-scheduler.sh)."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-scheduler.sh"


def _schedules() -> dict[str, str]:
    return dict(re.findall(r'^create_or_update_job "([^"]+)" "([^"]+)"', SCRIPT.read_text(), re.M))


def test_sunset_runs_with_the_poll():
    """Every job run wakes the Neon database, which bills while awake. The Sunset
    sweep shares the hourly poll's schedule so the two share one wake."""
    schedules = _schedules()
    assert schedules["claven-poll"] == "0 * * * *"
    assert schedules["claven-sunset"] == schedules["claven-poll"]
