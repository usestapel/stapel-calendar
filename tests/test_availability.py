"""Availability: free/busy and slot computation (the booking primitive)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar import services
from stapel_calendar.models import AvailabilityWindow, Participant, RSVP
from stapel_calendar.services import Interval, merge_intervals, subtract_intervals

UTC = ZoneInfo("UTC")


class TestIntervalMath:
    def test_merge_overlapping(self):
        merged = merge_intervals([
            Interval(datetime(2026, 1, 1, 9, tzinfo=UTC), datetime(2026, 1, 1, 10, tzinfo=UTC)),
            Interval(datetime(2026, 1, 1, 9, 30, tzinfo=UTC), datetime(2026, 1, 1, 11, tzinfo=UTC)),
        ])
        assert len(merged) == 1
        assert merged[0].end == datetime(2026, 1, 1, 11, tzinfo=UTC)

    def test_subtract_middle(self):
        window = Interval(
            datetime(2026, 1, 1, 9, tzinfo=UTC), datetime(2026, 1, 1, 17, tzinfo=UTC)
        )
        busy = [Interval(
            datetime(2026, 1, 1, 12, tzinfo=UTC), datetime(2026, 1, 1, 13, tzinfo=UTC)
        )]
        free = subtract_intervals(window, busy)
        assert len(free) == 2
        assert free[0].end == datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert free[1].start == datetime(2026, 1, 1, 13, tzinfo=UTC)


@pytest.mark.django_db
class TestFreeBusy:
    def test_busy_from_event(self, user):
        services.create_event(
            owner=user,
            title="Busy",
            start=datetime(2026, 1, 5, 10, tzinfo=UTC),
            end=datetime(2026, 1, 5, 11, tzinfo=UTC),
        )
        busy = services.free_busy(
            user, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 31, tzinfo=UTC)
        )
        assert len(busy) == 1

    def test_declined_is_not_busy(self, user, other_user):
        ev = services.create_event(
            owner=user,
            title="Optional",
            start=datetime(2026, 1, 5, 10, tzinfo=UTC),
            end=datetime(2026, 1, 5, 11, tzinfo=UTC),
            participant_ids=[str(other_user.id)],
        )
        Participant.objects.filter(event=ev, user=other_user).update(
            rsvp=RSVP.DECLINED
        )
        busy = services.free_busy(
            other_user, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 31, tzinfo=UTC)
        )
        assert busy == []

    def test_recurring_series_counts_all_occurrences(self, user):
        services.create_event(
            owner=user,
            title="Daily",
            start=datetime(2026, 1, 5, 10, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, 30, tzinfo=UTC),
            recurrence_type="daily",
        )
        busy = services.free_busy(
            user, datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 9, 23, tzinfo=UTC)
        )
        # Jan 5,6,7,8,9 (each 10:00) -> 5 busy intervals.
        assert len(busy) == 5


@pytest.mark.django_db
class TestSlots:
    def test_slots_from_window_minus_busy(self, user):
        # Monday 09:00-11:00 UTC working window.
        AvailabilityWindow.objects.create(
            user=user, weekday=0, start_time=time(9), end_time=time(11), timezone="UTC"
        )
        # 2026-01-05 is a Monday. Busy 09:30-10:00.
        services.create_event(
            owner=user,
            title="Busy",
            start=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        )
        slots = services.compute_slots(
            user,
            datetime(2026, 1, 5, 0, tzinfo=UTC),
            datetime(2026, 1, 6, 0, tzinfo=UTC),
            slot_minutes=30,
        )
        # Window 09:00-11:00 = 4 half-hour slots; the 09:30 one is busy -> 3.
        starts = [s.start.hour * 60 + s.start.minute for s in slots]
        assert 9 * 60 in starts  # 09:00
        assert 9 * 60 + 30 not in starts  # 09:30 busy
        assert 10 * 60 in starts  # 10:00
        assert 10 * 60 + 30 in starts  # 10:30
        assert len(slots) == 3

    def test_no_windows_no_slots(self, user):
        slots = services.compute_slots(
            user,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 6, tzinfo=UTC),
        )
        assert slots == []
