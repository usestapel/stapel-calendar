"""Regression tests for the 0.2.0 adversarial-review fixes.

Datasets are taken *literally* from tasks/fable/done/review-calendar-rrule.md:
DST transitions America/New_York 2026-03-08 (spring-forward gap) and
2026-11-01 (fall-back fold), the Pacific/Auckland range-edge window, the
2026-01-05..07 daily-series cancellation/reschedule cases, and the
slot_minutes DoS payloads.

Findings covered: H1 (DST gap/fold inverted-interval), H2 (PEP 495 dedup
miss), H3 (cancellation model — tombstone/EXDATE analog), H4 (slot_minutes
DoS), M1/M2 (naive UNTIL semantics), M3 (silent expansion cap), M4
(zero-duration grid shift), M5 (window-TZ day iteration), L (COUNT+UNTIL,
busy clipping, inverted-interval normalization).
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar import services
from stapel_calendar.models import AvailabilityWindow, Event, EventStatus
from stapel_calendar.recurrence import (
    InvalidRecurrence,
    build_rrule,
    expand_rule,
    expand_rule_detailed,
)
from stapel_calendar.services import Interval, merge_intervals, subtract_intervals

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MSK = ZoneInfo("Europe/Moscow")
AKL = ZoneInfo("Pacific/Auckland")


def _utc(dt):
    return dt.astimezone(UTC)


# ── H1: DST gap/fold — occurrence intervals must keep the exact duration ──


class TestH1DstIntervals:
    def test_spring_forward_gap_does_not_invert(self):
        """Daily 02:30-03:00 America/New_York across 2026-03-08 02:00->03:00.
        The gap occurrence was start=07:30Z end=07:00Z (-30 min)."""
        dtstart = datetime(2026, 3, 7, 2, 30, tzinfo=NY)
        occs = expand_rule(
            "FREQ=DAILY",
            dtstart,
            timedelta(minutes=30),
            dtstart,
            datetime(2026, 3, 9, 23, 0, tzinfo=NY),
        )
        gap = [o for o in occs if _utc(o.start).date() == datetime(2026, 3, 8).date()]
        assert len(gap) == 1
        occ = gap[0]
        assert _utc(occ.start) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
        assert _utc(occ.end) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)  # was 07:00Z
        # Every occurrence keeps the exact 30-minute duration.
        assert all(
            _utc(o.end) - _utc(o.start) == timedelta(minutes=30) for o in occs
        )

    def test_fall_back_fold_does_not_inflate(self):
        """Daily 01:30-02:00 NY across 2026-11-01 (02:00 EDT -> 01:00 EST).
        The fold occurrence was 05:30Z -> 07:00Z (1.5 h instead of 30 min)."""
        dtstart = datetime(2026, 10, 31, 1, 30, tzinfo=NY)
        occs = expand_rule(
            "FREQ=DAILY",
            dtstart,
            timedelta(minutes=30),
            dtstart,
            datetime(2026, 11, 2, 23, 0, tzinfo=NY),
        )
        fold = [o for o in occs if _utc(o.start).date() == datetime(2026, 11, 1).date()]
        assert len(fold) == 1
        occ = fold[0]
        assert _utc(occ.start) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        assert _utc(occ.end) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # was 07:00Z
        assert all(
            _utc(o.end) - _utc(o.start) == timedelta(minutes=30) for o in occs
        )

    @pytest.mark.django_db
    def test_materialize_gap_occurrence_persists_valid_interval(self, user):
        """materialize() used to persist the inverted row (row.end < row.start)."""
        series = services.create_event(
            owner=user,
            title="Gap",
            start=datetime(2026, 3, 7, 2, 30, tzinfo=NY),
            end=datetime(2026, 3, 7, 3, 0, tzinfo=NY),
            recurrence_type="daily",
        )
        occs = services.expand_event(
            series,
            datetime(2026, 3, 7, 0, 0, tzinfo=NY),
            datetime(2026, 3, 9, 0, 0, tzinfo=NY),
        )
        gap_start = occs[1].start  # 2026-03-08 02:30 NY (imaginary wall time)
        row = services.materialize(series, gap_start)
        row.refresh_from_db()
        assert row.end > row.start
        assert row.end - row.start == timedelta(minutes=30)

    def test_subtract_with_normalized_busy_yields_disjoint_free(self):
        """Review cascade: an inverted busy interval made subtract_intervals
        return overlapping free intervals (06:00-07:30 and 07:00-09:00 in a
        06:00-09:00Z window). Normalization drops the inverted interval."""
        window = Interval(
            datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
            datetime(2026, 3, 8, 9, 0, tzinfo=UTC),
        )
        inverted = Interval(
            datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
            datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        )
        busy = merge_intervals([inverted])
        assert busy == []  # defensively discarded, never reaches subtract
        free = subtract_intervals(window, busy)
        assert free == [window]


# ── H2: virtual-vs-materialized dedup across DST wall times (PEP 495) ─────


@pytest.mark.django_db
class TestH2Pep495Dedup:
    def _series(self, user, start_wall):
        return services.create_event(
            owner=user,
            title="DST dedup",
            start=start_wall,
            end=start_wall + timedelta(minutes=30),
            recurrence_type="daily",
        )

    def test_gap_instant_dedup(self, user):
        series = self._series(user, datetime(2026, 3, 7, 2, 30, tzinfo=NY))
        occs = services.expand_event(
            series,
            datetime(2026, 3, 7, 0, 0, tzinfo=NY),
            datetime(2026, 3, 9, 0, 0, tzinfo=NY),
        )
        row = services.materialize(series, occs[1].start)  # 2026-03-08 02:30 NY
        again = services.expand_event(
            series,
            datetime(2026, 3, 7, 0, 0, tzinfo=NY),
            datetime(2026, 3, 9, 0, 0, tzinfo=NY),
        )
        gap = [o for o in again if _utc(o.start) == _utc(occs[1].start)]
        assert len(gap) == 1
        assert gap[0].is_materialized  # was False — PEP 495 dict-key miss
        assert gap[0].materialized_id == row.id

    def test_ambiguous_instant_dedup(self, user):
        series = self._series(user, datetime(2026, 10, 31, 1, 30, tzinfo=NY))
        occs = services.expand_event(
            series,
            datetime(2026, 10, 31, 0, 0, tzinfo=NY),
            datetime(2026, 11, 2, 0, 0, tzinfo=NY),
        )
        fold_start = occs[1].start  # 2026-11-01 01:30 NY (ambiguous)
        row = services.materialize(series, fold_start)
        again = services.expand_event(
            series,
            datetime(2026, 10, 31, 0, 0, tzinfo=NY),
            datetime(2026, 11, 2, 0, 0, tzinfo=NY),
        )
        fold = [o for o in again if _utc(o.start) == _utc(fold_start)]
        assert len(fold) == 1
        assert fold[0].is_materialized
        assert fold[0].materialized_id == row.id

    def test_gap_instant_no_double_busy(self, user):
        """free_busy used to count the gap occurrence twice (virtual copy +
        concrete copy)."""
        series = self._series(user, datetime(2026, 3, 7, 2, 30, tzinfo=NY))
        occs = services.expand_event(
            series,
            datetime(2026, 3, 7, 0, 0, tzinfo=NY),
            datetime(2026, 3, 9, 0, 0, tzinfo=NY),
        )
        services.materialize(series, occs[1].start)
        busy = services.free_busy(
            user,
            datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
            datetime(2026, 3, 8, 9, 0, tzinfo=UTC),
        )
        assert busy == [
            Interval(
                datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
                datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
            )
        ]


# ── H3: cancellation model — tombstone (EXDATE analog) ────────────────────


def _daily_series(user, **kw):
    """Review dataset: daily series 10:00-11:00Z from 2026-01-05."""
    return services.create_event(
        owner=user,
        title="Daily",
        start=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        end=datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
        recurrence_type="daily",
        **kw,
    )


JAN7 = datetime(2026, 1, 7, 10, 0, tzinfo=UTC)
DAY7_START = datetime(2026, 1, 7, 0, 0, tzinfo=UTC)
DAY7_END = datetime(2026, 1, 8, 0, 0, tzinfo=UTC)


@pytest.mark.django_db
class TestH3CancellationModel:
    def test_api_delete_of_occurrence_tombstones_not_resurrects(
        self, api_client, user
    ):
        """DELETE of a materialized occurrence must not bring the virtual
        occurrence (and its busy slot) back."""
        series = _daily_series(user)
        row = services.materialize(series, JAN7)
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"/calendar/api/events/{row.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        row.refresh_from_db()  # tombstone kept, not deleted
        assert row.status == EventStatus.CANCELLED
        busy = services.free_busy(user, DAY7_START, DAY7_END)
        assert busy == []  # was busy 10:00-11:00Z again after DELETE

    def test_api_delete_of_standalone_still_deletes(self, api_client, user):
        ev = services.create_event(
            owner=user,
            title="One-off",
            start=JAN7,
            end=JAN7 + timedelta(hours=1),
        )
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"/calendar/api/events/{ev.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        assert not Event.objects.filter(id=ev.id).exists()

    def test_cancel_occurrence_frees_slot_and_suppresses_virtual(
        self, user, captured_events
    ):
        series = _daily_series(user)
        tomb = services.cancel_occurrence(series, JAN7)
        assert tomb.status == EventStatus.CANCELLED
        assert tomb.recurrence_id == JAN7
        # No busy interval on Jan 7 — neither concrete nor virtual.
        assert services.free_busy(user, DAY7_START, DAY7_END) == []
        # Expansion omits the instant entirely.
        occs = services.expand_event(series, DAY7_START, DAY7_END)
        assert occs == []
        # A tombstone must NOT trigger app-layer resource creation.
        assert not any(
            e.event_type == "calendar.occurrence.materialized"
            for e in captured_events
        )
        # Idempotent.
        assert services.cancel_occurrence(series, JAN7).id == tomb.id

    def test_cancelled_event_status_not_busy(self, user):
        """free_busy did not filter status at all — a cancelled event stayed
        busy."""
        series = _daily_series(user)
        row = services.materialize(series, JAN7)
        row.status = EventStatus.CANCELLED
        row.save(update_fields=["status"])
        assert services.free_busy(user, DAY7_START, DAY7_END) == []

    def test_cancelled_standalone_and_series_not_busy(self, user):
        services.create_event(
            owner=user,
            title="Cancelled one-off",
            start=JAN7,
            end=JAN7 + timedelta(hours=1),
            status=EventStatus.CANCELLED,
        )
        _daily_series(user, status=EventStatus.CANCELLED)
        assert services.free_busy(user, DAY7_START, DAY7_END) == []

    def test_rescheduled_occurrence_does_not_double_busy(self, user):
        """Occurrence 2026-01-07 10:00Z moved to 15:00Z: busy must be
        [15:00-16:00] only, not also [10:00-11:00]."""
        series = _daily_series(user)
        row = services.materialize(series, JAN7)
        row.start = datetime(2026, 1, 7, 15, 0, tzinfo=UTC)
        row.end = datetime(2026, 1, 7, 16, 0, tzinfo=UTC)
        row.save(update_fields=["start", "end"])
        busy = services.free_busy(user, DAY7_START, DAY7_END)
        assert busy == [
            Interval(
                datetime(2026, 1, 7, 15, 0, tzinfo=UTC),
                datetime(2026, 1, 7, 16, 0, tzinfo=UTC),
            )
        ]
        # Expansion reports the moved occurrence at its actual time, still
        # linked to the concrete row.
        occs = services.expand_event(series, DAY7_START, DAY7_END)
        assert len(occs) == 1
        assert occs[0].is_materialized
        assert occs[0].materialized_id == row.id
        assert occs[0].start == datetime(2026, 1, 7, 15, 0, tzinfo=UTC)

    def test_materialize_rejects_off_rule_instant(self, user):
        """2026-01-07 10:17Z is not an instant of the daily-10:00 rule — it
        used to create a ghost row (invisible to expand, counted busy)."""
        series = _daily_series(user)
        with pytest.raises(InvalidRecurrence):
            services.materialize(series, datetime(2026, 1, 7, 10, 17, tzinfo=UTC))
        # Explicit escape hatch for deliberate exception occurrences.
        row = services.materialize(
            series, datetime(2026, 1, 7, 10, 17, tzinfo=UTC), off_rule=True
        )
        assert row.recurrence_parent_id == series.id
        # Idempotent second call for the existing off-rule row.
        again = services.materialize(
            series, datetime(2026, 1, 7, 10, 17, tzinfo=UTC), off_rule=True
        )
        assert again.id == row.id

    def test_materialize_rejects_instant_past_series_end(self, user):
        series = services.create_event(
            owner=user,
            title="Bounded",
            start=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
            recurrence_type="daily",
            recurrence_count=3,  # Jan 5, 6, 7
        )
        with pytest.raises(InvalidRecurrence):
            services.materialize(series, datetime(2026, 1, 8, 10, 0, tzinfo=UTC))


# ── H4: slot_minutes DoS ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestH4SlotMinutesDos:
    @pytest.fixture(autouse=True)
    def _window(self, user):
        # Review dataset: window Mon 09-10 UTC, range 2026-01-05 (a Monday).
        AvailabilityWindow.objects.create(
            user=user, weekday=0, start_time=time(9), end_time=time(10),
            timezone="UTC",
        )

    @pytest.mark.parametrize("bad", [0, -30])
    def test_compute_slots_rejects_non_positive_step(self, user, bad):
        with pytest.raises(ValueError):
            services.compute_slots(
                user,
                datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
                datetime(2026, 1, 6, 0, 0, tzinfo=UTC),
                slot_minutes=bad,
            )

    @pytest.mark.parametrize("bad", ["0", "-5", "abc"])
    def test_availability_view_rejects_bad_slot_minutes(self, auth_client, bad):
        resp = auth_client.get(
            "/calendar/api/availability"
            f"?start=2026-01-05T00:00:00Z&end=2026-01-06T00:00:00Z&slot_minutes={bad}"
        )
        assert resp.status_code == 400
        assert (
            resp.json()["localizable_error"]
            == "error.400.calendar_invalid_slot_minutes"
        )


# ── M1/M2: UNTIL timezone semantics ───────────────────────────────────────


class TestUntilSemantics:
    def test_naive_until_interpreted_in_series_timezone(self):
        """Review dataset: daily 04:00 Europe/Moscow from 2026-01-08, naive
        until 2026-01-10 03:00 (meant MSK => 2026-01-10T00:00Z). Blind Z
        stamping produced UNTIL=20260110T030000Z and a third occurrence."""
        rule = build_rrule(
            "daily",
            until=datetime(2026, 1, 10, 3, 0),
            dtstart=datetime(2026, 1, 8, 4, 0, tzinfo=MSK),
        )
        assert "UNTIL=20260110T000000Z" in rule

    def test_naive_until_end_to_end_two_occurrences_not_three(self):
        dtstart = datetime(2026, 1, 8, 4, 0, tzinfo=MSK)
        rule = build_rrule(
            "daily", until=datetime(2026, 1, 10, 3, 0), dtstart=dtstart
        )
        starts = [
            _utc(o.start)
            for o in expand_rule(
                rule,
                dtstart,
                timedelta(hours=1),
                dtstart,
                datetime(2026, 1, 12, tzinfo=MSK),
            )
        ]
        # Jan 8 and 9 only — Jan 10 04:00 MSK (01:00Z) is past 00:00Z UNTIL.
        assert [s.day for s in starts] == [8, 9]

    def test_naive_until_with_naive_dtstart_stays_floating(self):
        rule = build_rrule(
            "daily",
            until=datetime(2026, 1, 10, 3, 0),
            dtstart=datetime(2026, 1, 8, 4, 0),
        )
        assert "UNTIL=20260110T030000" in rule
        assert "Z" not in rule.split("UNTIL=")[1]

    def test_naive_until_without_dtstart_rejected(self):
        with pytest.raises(InvalidRecurrence):
            build_rrule("daily", until=datetime(2026, 1, 10, 3, 0))

    def test_aware_until_with_naive_dtstart_rejected_at_build_time(self):
        """M2: this combination used to save fine and then poison every
        expand with dateutil's UNTIL-must-be-UTC ValueError (500s)."""
        with pytest.raises(InvalidRecurrence):
            build_rrule(
                "daily",
                until=datetime(2026, 1, 10, 0, 0, tzinfo=UTC),
                dtstart=datetime(2026, 1, 8, 4, 0),
            )

    @pytest.mark.django_db
    def test_create_event_naive_start_aware_until_is_400_material(self, user):
        with pytest.raises(InvalidRecurrence):
            services.create_event(
                owner=user,
                title="Poisoned",
                start=datetime(2026, 1, 8, 4, 0),
                end=datetime(2026, 1, 8, 5, 0),
                recurrence_type="daily",
                recurrence_until=datetime(2026, 1, 10, 0, 0, tzinfo=UTC),
            )

    def test_count_and_until_mutually_exclusive(self):
        """RFC 5545 §3.3.10 forbids COUNT and UNTIL in one RRULE."""
        with pytest.raises(InvalidRecurrence):
            build_rrule(
                "daily", count=5, until=datetime(2026, 2, 1, tzinfo=UTC)
            )


# ── M3: expansion cap must not be silent for availability ─────────────────


@pytest.mark.django_db
class TestM3TruncationFlag:
    def test_free_busy_detailed_flags_capped_expansion(self, user, settings):
        """Review dataset: unbounded daily series, range 2026-01-01 ->
        2030-01-01 — with the default cap of 1000 the expansion stops at
        2028-09-26 and everything after silently looked free."""
        services.create_event(
            owner=user,
            title="Unbounded",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            recurrence_type="daily",
        )
        result = services.free_busy_detailed(
            user,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2030, 1, 1, tzinfo=UTC),
        )
        assert result.truncated
        assert len(result.busy) == 1000
        assert result.busy[-1].start == datetime(2028, 9, 26, 10, 0, tzinfo=UTC)

    def test_not_truncated_when_range_fits_cap(self, user):
        services.create_event(
            owner=user,
            title="Short",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            recurrence_type="daily",
        )
        result = services.free_busy_detailed(
            user,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 10, tzinfo=UTC),
        )
        assert not result.truncated

    def test_expand_rule_detailed_exact_cap_boundary_not_truncated(self):
        """COUNT=5 with cap 5: the cap is reached but nothing was cut."""
        dtstart = datetime(2026, 1, 1, tzinfo=UTC)
        expansion = expand_rule_detailed(
            "FREQ=DAILY;COUNT=5",
            dtstart,
            timedelta(hours=1),
            dtstart,
            datetime(2026, 2, 1, tzinfo=UTC),
            max_occurrences=5,
        )
        assert len(expansion.occurrences) == 5
        assert not expansion.truncated

    def test_availability_api_reports_truncated(self, auth_client, user, settings):
        settings.STAPEL_CALENDAR = {"MAX_EXPANSION_OCCURRENCES": 3}
        services.create_event(
            owner=user,
            title="Capped",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            recurrence_type="daily",
        )
        resp = auth_client.get(
            "/calendar/api/availability"
            "?start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:00Z"
        )
        assert resp.status_code == 200
        assert resp.json()["truncated"] is True

    def test_free_busy_function_reports_truncated(self, user, settings):
        from stapel_core.comm import call

        settings.STAPEL_CALENDAR = {"MAX_EXPANSION_OCCURRENCES": 3}
        services.create_event(
            owner=user,
            title="Capped",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            recurrence_type="daily",
        )
        result = call(
            "calendar.free_busy",
            {
                "user_id": str(user.id),
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-02-01T00:00:00+00:00",
            },
        )
        assert result["truncated"] is True
        assert len(result["busy"]) == 3


# ── M4: zero-duration events must not shift the slot grid ─────────────────


@pytest.mark.django_db
class TestM4ZeroDuration:
    def test_zero_duration_event_does_not_shift_grid(self, user):
        """Window Mon 09:00-12:00 UTC (2026-01-05), 60-min slots, a
        zero-duration event at 09:30 — slots must stay [09:00, 10:00, 11:00]
        (the buggy grid was [09:30, 10:30] and 09:00 was lost)."""
        AvailabilityWindow.objects.create(
            user=user, weekday=0, start_time=time(9), end_time=time(12),
            timezone="UTC",
        )
        services.create_event(
            owner=user,
            title="Marker",
            start=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
        )
        slots = services.compute_slots(
            user,
            datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 6, 0, 0, tzinfo=UTC),
            slot_minutes=60,
        )
        assert [s.start.hour for s in slots] == [9, 10, 11]

    def test_zero_duration_event_not_busy(self, user):
        services.create_event(
            owner=user,
            title="Marker",
            start=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
        )
        assert services.free_busy(
            user,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 6, tzinfo=UTC),
        ) == []

    def test_create_event_rejects_inverted_range(self, user):
        with pytest.raises(ValueError):
            services.create_event(
                owner=user,
                title="Backwards",
                start=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
                end=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            )


# ── M5: slot days iterate in the window's timezone ─────────────────────────


@pytest.mark.django_db
class TestM5WindowTimezoneDays:
    def test_auckland_window_on_range_edge_not_dropped(self, user):
        """Window Mon 08:00-10:00 Pacific/Auckland; range 2026-06-01T00:00Z ..
        2026-06-07T23:59Z contains the Auckland Monday June 8 08:00-10:00 NZST
        (= June 7 20:00-22:00Z) — it used to yield 0 slots."""
        AvailabilityWindow.objects.create(
            user=user, weekday=0, start_time=time(8), end_time=time(10),
            timezone="Pacific/Auckland",
        )
        slots = services.compute_slots(
            user,
            datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 7, 23, 59, tzinfo=UTC),
            slot_minutes=60,
        )
        assert [(_utc(s.start), _utc(s.end)) for s in slots] == [
            (
                datetime(2026, 6, 7, 20, 0, tzinfo=UTC),
                datetime(2026, 6, 7, 21, 0, tzinfo=UTC),
            ),
            (
                datetime(2026, 6, 7, 21, 0, tzinfo=UTC),
                datetime(2026, 6, 7, 22, 0, tzinfo=UTC),
            ),
        ]


# ── L: busy clipping + interval hygiene ────────────────────────────────────


@pytest.mark.django_db
class TestBusyClipping:
    def test_busy_clipped_to_requested_range(self, user):
        """Event 08:00-10:00Z, range from 09:00Z: only the in-range part."""
        services.create_event(
            owner=user,
            title="Early",
            start=datetime(2026, 1, 5, 8, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        )
        busy = services.free_busy(
            user,
            datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            datetime(2026, 1, 5, 17, 0, tzinfo=UTC),
        )
        assert busy == [
            Interval(
                datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
                datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
            )
        ]

    def test_touching_event_outside_range_excluded(self, user):
        """Event 08:00-09:00Z for a range starting 09:00Z — was returned
        whole, now clips to nothing."""
        services.create_event(
            owner=user,
            title="Before",
            start=datetime(2026, 1, 5, 8, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        )
        busy = services.free_busy(
            user,
            datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            datetime(2026, 1, 5, 17, 0, tzinfo=UTC),
        )
        assert busy == []
