"""Neon free-plan usage monitor (scripts/neon_usage.py)."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "neon_usage.py"
_spec = importlib.util.spec_from_file_location("neon_usage", _PATH)
neon_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(neon_usage)

ORG = "org-claven"
PROJECT = "proj-claven"
GB = 10**9


def _api(*, active_hours=0.0, transfer_bytes=0, plan="free", org_id=ORG,
         max_cus=(0.25,), logical_sizes=(36 * 10**6,),
         start="2026-11-01T00:00:00Z", end="2026-12-01T00:00:00Z"):
    """Neon API responses, shaped like the real ones, for one project."""
    return {
        "org": {"id": ORG, "plan": plan},
        "project": {
            "id": PROJECT,
            "org_id": org_id,
            "active_time_seconds": int(active_hours * 3600),
            "data_transfer_bytes": transfer_bytes,
            "consumption_period_start": start,
            "consumption_period_end": end,
            "branch_logical_size_limit_bytes": 1024**3,
        },
        "endpoints": [{"id": f"ep-{i}", "autoscaling_limit_max_cu": cu} for i, cu in enumerate(max_cus)],
        "branches": [{"name": f"b{i}", "logical_size": s} for i, s in enumerate(logical_sizes)],
    }


def _assess(now="2026-11-11T00:00:00Z", **kw):
    """Findings for a project ten days into a 30-day period, unless overridden."""
    snap = neon_usage.snapshot(**_api(**kw))
    return neon_usage.assess(snap, expected_org=ORG, now=datetime.fromisoformat(now))


def _levels(findings):
    return {f.metric: f.level for f in findings}


class TestSnapshot:
    def test_compute_is_awake_time_at_the_largest_compute_size(self):
        snap = neon_usage.snapshot(**_api(active_hours=40, max_cus=(0.25, 0.5)))
        assert snap.compute_cu_hours == pytest.approx(20.0)

    def test_storage_is_the_largest_branch(self):
        snap = neon_usage.snapshot(**_api(logical_sizes=(100, 300, 200)))
        assert snap.storage_bytes == 300
        assert snap.storage_limit_bytes == 1024**3

    def test_period_and_plan(self):
        snap = neon_usage.snapshot(**_api(plan="launch"))
        assert snap.plan == "launch"
        assert snap.period_start == datetime(2026, 11, 1, tzinfo=timezone.utc)
        assert snap.period_end == datetime(2026, 12, 1, tzinfo=timezone.utc)


class TestProjection:
    def test_projects_the_period_average_to_the_period_end(self):
        snap = neon_usage.snapshot(**_api(active_hours=40))  # 10 CU-h in 10 days
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        assert neon_usage.projected(snap.compute_cu_hours, snap, now) == pytest.approx(30.0)

    def test_first_hours_of_a_period_do_not_explode(self):
        """Ten minutes in, a few minutes awake must not project to thousands of hours."""
        snap = neon_usage.snapshot(**_api(active_hours=0.1))
        now = datetime(2026, 11, 1, 0, 10, tzinfo=timezone.utc)
        assert neon_usage.projected(snap.compute_cu_hours, snap, now) < 100


class TestAssess:
    def test_quiet_month_is_ok(self):
        findings = _assess(active_hours=40, transfer_bytes=GB // 10)
        assert {f.level for f in findings} == {"ok"}

    def test_compute_on_pace_for_60_percent_warns(self):
        # 20 CU-h in 10 of 30 days projects to 60 CU-h of 100.
        assert _levels(_assess(active_hours=80))["compute"] == "warn"

    def test_compute_on_pace_for_80_percent_alerts(self):
        # 27 CU-h in 10 of 30 days projects to 81 CU-h of 100.
        assert _levels(_assess(active_hours=108))["compute"] == "alert"

    def test_compute_used_up_alerts_even_late_in_the_period(self):
        assert _levels(_assess(now="2026-11-30T12:00:00Z", active_hours=330))["compute"] == "alert"

    def test_transfer_on_pace_for_80_percent_alerts(self):
        assert _levels(_assess(transfer_bytes=int(1.4 * GB)))["transfer"] == "alert"

    def test_storage_near_the_limit_alerts(self):
        assert _levels(_assess(logical_sizes=(int(0.85 * 1024**3),)))["storage"] == "alert"

    def test_storage_past_half_warns(self):
        assert _levels(_assess(logical_sizes=(int(0.55 * 1024**3),)))["storage"] == "warn"

    def test_paid_plan_alerts(self):
        """A paid plan means the org can bill; Claven is meant to stay on Free."""
        assert _levels(_assess(plan="launch"))["plan"] == "alert"

    def test_project_moved_out_of_the_org_alerts(self):
        assert _levels(_assess(org_id="org-other"))["org"] == "alert"

    def test_compute_above_the_cap_alerts(self):
        assert _levels(_assess(max_cus=(0.25, 1)))["compute_size"] == "alert"

    def test_second_compute_warns(self):
        """A second compute (a dev branch) spends the same monthly hours."""
        assert _levels(_assess(max_cus=(0.25, 0.25)))["computes"] == "warn"

    def test_every_finding_says_what_it_measured(self):
        for f in _assess(active_hours=108):
            assert f.message


class TestReport:
    def test_report_leads_with_the_worst_level_and_shows_compute(self):
        snap = neon_usage.snapshot(**_api(active_hours=108))
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        findings = neon_usage.assess(snap, expected_org=ORG, now=now)
        report = neon_usage.report(snap, findings, now)
        assert report.splitlines()[0].startswith("## 🔴")
        assert "27.0 of 100 CU-hours" in report

    def test_worst_level(self):
        F = neon_usage.Finding
        assert neon_usage.worst([F("a", "ok", "x"), F("b", "warn", "y")]) == "warn"
        assert neon_usage.worst([F("a", "alert", "x"), F("b", "warn", "y")]) == "alert"
        assert neon_usage.worst([F("a", "ok", "x")]) == "ok"
