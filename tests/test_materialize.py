"""Virtual expansion vs on-demand materialization, and the resource-decoupling
invariant: the engine emits `calendar.occurrence.materialized` and creates NO
app resource itself (the coupling this extraction removes)."""
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar import services
from stapel_calendar.models import Event, RSVP

UTC = ZoneInfo("UTC")


def _series(owner, **kw):
    return services.create_event(
        owner=owner,
        title="Standup",
        start=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
        recurrence_type="weekly",
        **kw,
    )


@pytest.mark.django_db
class TestVirtualExpansion:
    def test_expand_does_not_persist(self, user):
        series = _series(user)
        before = Event.objects.count()
        occs = services.expand_event(
            series,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 2, 1, tzinfo=UTC),
        )
        # Weekly for ~4 weeks in January.
        assert len(occs) >= 4
        assert all(not o.is_materialized for o in occs)
        assert Event.objects.count() == before  # nothing persisted

    def test_standalone_event_expands_to_itself(self, user):
        ev = services.create_event(
            owner=user,
            title="One-off",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        )
        occs = services.expand_event(
            ev, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )
        assert len(occs) == 1
        assert occs[0].is_materialized


@pytest.mark.django_db
class TestMaterialize:
    def test_materialize_persists_once(self, user):
        series = _series(user)
        occ_start = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        occ1 = services.materialize(series, occ_start)
        occ2 = services.materialize(series, occ_start)  # idempotent
        assert occ1.id == occ2.id
        assert occ1.recurrence_parent_id == series.id
        assert occ1.rrule == ""
        assert series.occurrences.count() == 1

    def test_materialize_copies_participants_in_batch(self, user, other_user):
        series = _series(user, participant_ids=[str(other_user.id)])
        occ = services.materialize(series, datetime(2026, 1, 12, 9, tzinfo=UTC))
        rsvps = {p.user_id: p.rsvp for p in occ.participants.all()}
        assert rsvps[user.id] == RSVP.ACCEPTED
        assert rsvps[other_user.id] == RSVP.INVITED

    def test_expand_flags_materialized(self, user):
        series = _series(user)
        occ_start = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        services.materialize(series, occ_start)
        occs = services.expand_event(
            series, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )
        materialized = [o for o in occs if o.is_materialized]
        assert len(materialized) == 1
        assert materialized[0].start == occ_start

    def test_engine_creates_no_app_resource(self, user, captured_events):
        """The recurrence engine must ONLY create Event + Participant rows and
        emit the hook — never an app resource (legacy's Room is an app-layer
        subscriber's job). We assert exactly one occurrence event and the emit."""
        series = _series(user, participant_ids=[])
        n_events_before = Event.objects.count()
        services.materialize(series, datetime(2026, 1, 12, 9, tzinfo=UTC))
        # Exactly one new Event (the occurrence); no cascade of other rows.
        assert Event.objects.count() == n_events_before + 1
        names = [e.event_type for e in captured_events]
        assert "calendar.occurrence.materialized" in names

    def test_materialized_hook_payload(self, user, captured_events):
        series = _series(user)
        occ = services.materialize(series, datetime(2026, 1, 12, 9, tzinfo=UTC))
        evt = next(
            e for e in captured_events
            if e.event_type == "calendar.occurrence.materialized"
        )
        assert evt.payload["series_id"] == str(series.id)
        assert evt.payload["event_id"] == str(occ.id)

    def test_concurrent_materialize_returns_winner_row(self, user, captured_events):
        """The concurrent-booking case: two materialize calls for the same
        slot. The winner creates + emits; the loser's create() hits the
        (recurrence_parent, start) unique constraint, catches IntegrityError,
        re-queries and returns the winner's row — no duplicate, no 500, no
        second emit."""
        series = _series(user)
        occ_start = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        winner = services.materialize(series, occ_start)  # first (winner)
        n_events = Event.objects.count()

        # Force the loser's pre-check to miss so create() actually runs and
        # collides with the winner's row; the except path then re-queries.
        with mock.patch.object(
            services, "_find_occurrence", side_effect=[None, winner]
        ) as finder:
            loser = services.materialize(series, occ_start)

        assert loser.id == winner.id  # returned the winner's row, not a raise
        assert Event.objects.count() == n_events  # no duplicate occurrence
        assert finder.call_count == 2  # pre-check miss + post-conflict re-query
        # Exactly one occurrence.materialized emit across both calls (winner's).
        names = [e.event_type for e in captured_events]
        assert names.count("calendar.occurrence.materialized") == 1


@pytest.mark.django_db
class TestFreeBusyDedup:
    def test_materialized_not_double_counted(self, user):
        series = _series(user)
        # Materialize one occurrence; free/busy must not count it twice.
        occ_start = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        services.materialize(series, occ_start)
        busy = services.free_busy(
            user, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )
        # No interval appears twice for the materialized slot.
        matching = [b for b in busy if b.start == occ_start]
        assert len(matching) == 1
