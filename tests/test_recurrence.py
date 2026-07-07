"""Recurrence engine correctness — the extraction's highest-risk area.

The prior source used ``timedelta(days=30)`` for monthly (wrong) and a
raw CSV for custom weekdays. These tests pin the RFC 5545 / dateutil
behavior, including the month-end and DST edge cases that broke the source.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar.recurrence import (
    InvalidRecurrence,
    build_rrule,
    expand_rule,
    normalize_weekdays,
    register_preset,
)

UTC = ZoneInfo("UTC")


def _starts(rrule, dtstart, start, end):
    return [o.start for o in expand_rule(rrule, dtstart, timedelta(hours=1), start, end)]


class TestBuildRrule:
    def test_none_is_empty(self):
        assert build_rrule("none") == ""

    def test_daily(self):
        assert build_rrule("daily") == "FREQ=DAILY"

    def test_weekdays(self):
        assert build_rrule("weekdays") == "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"

    def test_weekly(self):
        assert build_rrule("weekly") == "FREQ=WEEKLY"

    def test_biweekly_uses_interval(self):
        assert build_rrule("biweekly") == "FREQ=WEEKLY;INTERVAL=2"

    def test_monthly(self):
        assert build_rrule("monthly") == "FREQ=MONTHLY"

    def test_custom_requires_weekdays(self):
        with pytest.raises(InvalidRecurrence):
            build_rrule("custom")

    def test_custom_byday_from_ints(self):
        # 0=Mon, 2=Wed, 4=Fri
        assert build_rrule("custom", byweekday=[0, 2, 4]) == "FREQ=WEEKLY;BYDAY=MO,WE,FR"

    def test_custom_byday_from_csv(self):
        assert build_rrule("custom", byweekday="4,0,2") == "FREQ=WEEKLY;BYDAY=MO,WE,FR"

    def test_unknown_preset(self):
        with pytest.raises(InvalidRecurrence):
            build_rrule("fortnightly-ish")

    def test_count_and_until(self):
        rule = build_rrule("daily", count=5)
        assert "COUNT=5" in rule
        until = datetime(2026, 2, 1, tzinfo=UTC)
        assert "UNTIL=20260201T000000Z" in build_rrule("daily", until=until)


class TestNormalizeWeekdays:
    def test_csv(self):
        assert normalize_weekdays("0,2,4") == [0, 2, 4]

    def test_dedup_sorted(self):
        assert normalize_weekdays([4, 0, 0, 2]) == [0, 2, 4]

    def test_out_of_range(self):
        with pytest.raises(InvalidRecurrence):
            normalize_weekdays([7])


class TestMonthEndEdgeCases:
    """The bug the source had: monthly = +30 days drifts off the day-of-month.
    RFC 5545 FREQ=MONTHLY anchored on the 31st SKIPS months with no 31st."""

    def test_jan31_skips_february(self):
        dtstart = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
        starts = _starts(
            "FREQ=MONTHLY", dtstart, dtstart, datetime(2026, 6, 1, tzinfo=UTC)
        )
        days = [s.day for s in starts]
        months = [s.month for s in starts]
        # Jan 31, Mar 31, May 31 — Feb and Apr (no 31st) are skipped.
        assert months == [1, 3, 5]
        assert set(days) == {31}

    def test_timedelta30_would_have_been_wrong(self):
        """Contrast: the old +30d logic would land on Mar 2 (31+30 days),
        drifting off day-of-month. The rrule keeps the 31st."""
        dtstart = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)
        wrong = (dtstart + timedelta(days=30)).day  # -> Mar 2
        assert wrong == 2
        starts = _starts(
            "FREQ=MONTHLY", dtstart, dtstart, datetime(2026, 4, 1, tzinfo=UTC)
        )
        # Correct next occurrence is Mar 31, not Mar 2.
        assert starts[1] == datetime(2026, 3, 31, 9, 0, tzinfo=UTC)

    def test_month_31_full_year(self):
        dtstart = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
        starts = _starts(
            "FREQ=MONTHLY", dtstart, dtstart, datetime(2026, 12, 31, 23, tzinfo=UTC)
        )
        months = [s.month for s in starts]
        # Only months with 31 days.
        assert months == [1, 3, 5, 7, 8, 10, 12]


class TestDST:
    """Across a US spring-forward (2026-03-08 02:00 EST->EDT), a daily 09:00
    series keeps 09:00 *wall-clock* — the UTC offset shifts, the local hour
    does not."""

    def test_daily_keeps_wall_clock_across_dst(self):
        ny = ZoneInfo("America/New_York")
        dtstart = datetime(2026, 3, 6, 9, 0, tzinfo=ny)
        starts = _starts(
            "FREQ=DAILY", dtstart, dtstart, datetime(2026, 3, 12, tzinfo=ny)
        )
        # Every occurrence is 09:00 local.
        assert all(s.astimezone(ny).hour == 9 for s in starts)
        # The UTC offset changed across the transition (EST -14400 -> EDT -18000... ).
        before = starts[0].utcoffset()
        after = starts[-1].utcoffset()
        assert before != after


class TestExpansionCap:
    def test_max_occurrences(self):
        dtstart = datetime(2026, 1, 1, tzinfo=UTC)
        occs = expand_rule(
            "FREQ=DAILY",
            dtstart,
            timedelta(hours=1),
            dtstart,
            datetime(2030, 1, 1, tzinfo=UTC),
            max_occurrences=10,
        )
        assert len(occs) == 10

    def test_cap_bounds_computation_not_just_result(self):
        """An unbounded daily rule over a ~1000-year range must return exactly
        max_occurrences and stop — the lazy xafter iteration means we never
        compute the ~365k intervals the full range would contain."""
        dtstart = datetime(2026, 1, 1, tzinfo=UTC)
        occs = expand_rule(
            "FREQ=DAILY",
            dtstart,
            timedelta(hours=1),
            dtstart,
            datetime(3000, 1, 1, tzinfo=UTC),  # enormous range end
            max_occurrences=5,
        )
        assert len(occs) == 5
        assert occs[-1].start == datetime(2026, 1, 5, tzinfo=UTC)


class TestPresetRegistry:
    """Extension seam #4 — custom recurrence presets beyond the built-ins."""

    def test_register_custom_preset(self):
        from dateutil.rrule import WEEKLY

        register_preset("triweekly", {"freq": WEEKLY, "interval": 3})
        assert build_rrule("triweekly") == "FREQ=WEEKLY;INTERVAL=3"

    def test_settings_presets_merge_over_builtins(self, settings):
        from dateutil.rrule import DAILY

        settings.STAPEL_CALENDAR = {"PRESETS": {"everyday": {"freq": DAILY}}}
        assert build_rrule("everyday") == "FREQ=DAILY"
        # built-ins still present
        assert build_rrule("weekly") == "FREQ=WEEKLY"

    def test_none_removes_builtin(self, settings):
        settings.STAPEL_CALENDAR = {"PRESETS": {"monthly": None}}
        with pytest.raises(InvalidRecurrence):
            build_rrule("monthly")
