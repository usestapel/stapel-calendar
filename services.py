"""Domain services for stapel-calendar.

The generic calendar core: event/participant management, RSVP, recurrence
expansion (virtual) + materialization (on-demand), and availability
(free/busy + slot computation). No app resources are created here — when an
occurrence is materialized the engine *emits* ``calendar.occurrence.materialized``
and the app-layer subscriber owns any resource creation (the coupling this
extraction removes: legacy used to create a ``rooms.Room`` inside the
recurrence loop).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from stapel_core.comm import emit

from .models import BUSY_RSVP, Event, Participant, RSVP
from .recurrence import Occurrence, build_rrule, expand_rule


# ── Event & participant management ──────────────────────────────────────


def create_event(
    *,
    owner,
    title: str,
    start: datetime,
    end: datetime,
    description: str = "",
    scope_key: str = "",
    status: str | None = None,
    recurrence_type: str = "none",
    recurrence_interval: int | None = None,
    recurrence_weekdays=None,
    recurrence_until: datetime | None = None,
    recurrence_count: int | None = None,
    participant_ids=None,
) -> Event:
    """Create an event (series master if recurring), add the owner as an
    accepted participant and any invitees in one batch."""
    from .models import EventStatus

    rrule_text = build_rrule(
        recurrence_type,
        interval=recurrence_interval,
        byweekday=recurrence_weekdays,
        until=recurrence_until,
        count=recurrence_count,
    )
    event = Event.objects.create(
        owner=owner,
        title=title,
        description=description or "",
        start=start,
        end=end,
        scope_key=scope_key or "",
        status=status or EventStatus.CONFIRMED,
        rrule=rrule_text,
        recurrence_type=recurrence_type,
    )
    _set_participants(event, owner, participant_ids or [])
    return event


def _set_participants(event: Event, owner, participant_ids) -> None:
    """Batch-create participants: owner accepted, the rest invited. Replaces
    legacy's per-loop ``create`` calls."""
    rows = [Participant(event=event, user=owner, rsvp=RSVP.ACCEPTED)]
    seen = {str(owner.pk)}
    for uid in participant_ids:
        if str(uid) in seen:
            continue
        seen.add(str(uid))
        rows.append(Participant(event=event, user_id=uid, rsvp=RSVP.INVITED))
    Participant.objects.bulk_create(rows, ignore_conflicts=True)


def respond(event: Event, user, rsvp: str) -> Participant:
    """Record a participant's RSVP. Raises ``Participant.DoesNotExist`` if
    the user was never invited."""
    participant = Participant.objects.get(event=event, user=user)
    participant.rsvp = rsvp
    participant.save(update_fields=["rsvp", "updated_at"])
    return participant


# ── Recurrence: virtual expansion + on-demand materialization ───────────


def expand_event(
    event: Event, range_start: datetime, range_end: datetime
) -> list[Occurrence]:
    """Virtually expand a series master over a range. Occurrences already
    materialized (a child row exists at that start) are flagged and carry
    their concrete id; the rest are virtual and never persisted."""
    if not event.rrule:
        # A concrete/standalone event contributes itself if it intersects.
        if event.start <= range_end and event.end >= range_start:
            return [
                Occurrence(
                    event_id=event.id,
                    start=event.start,
                    end=event.end,
                    is_materialized=True,
                    materialized_id=event.id,
                )
            ]
        return []

    materialized = {
        row.start: row.id
        for row in event.occurrences.all().only("id", "start")
    }
    occs = expand_rule(
        event.rrule,
        event.start,
        event.duration,
        range_start,
        range_end,
        event_id=event.id,
    )
    out: list[Occurrence] = []
    for occ in occs:
        mat_id = materialized.get(occ.start)
        out.append(
            Occurrence(
                event_id=event.id,
                start=occ.start,
                end=occ.end,
                is_materialized=mat_id is not None,
                materialized_id=mat_id,
            )
        )
    return out


def _find_occurrence(series: Event, occurrence_start: datetime):
    return series.occurrences.filter(start=occurrence_start).first()


def materialize(series: Event, occurrence_start: datetime) -> Event:
    """Persist a single occurrence of ``series`` so it can gain its own state
    (RSVP, resource). Idempotent: a second call for the same start returns
    the existing row. Emits ``calendar.occurrence.materialized`` (and the
    ``occurrence_materialized`` Django signal) — this engine creates **no**
    app resource itself.

    Concurrency-safe: the ``(recurrence_parent, start)`` partial unique
    constraint guarantees at most one occurrence per instant. Two concurrent
    materialize calls for the same slot (the normal concurrent-booking case)
    both see no existing row, race to ``create()``, and the loser catches the
    ``IntegrityError``, re-queries and returns the winner's row — **without**
    re-emitting the event or re-sending the signal (the winner already did).
    """
    if not series.is_series_master:
        raise ValueError("can only materialize occurrences of a series master")

    existing = _find_occurrence(series, occurrence_start)
    if existing is not None:
        return existing

    occurrence_end = occurrence_start + series.duration
    try:
        with transaction.atomic():
            occurrence = Event.objects.create(
                owner=series.owner,
                title=series.title,
                description=series.description,
                start=occurrence_start,
                end=occurrence_end,
                scope_key=series.scope_key,
                status=series.status,
                rrule="",
                recurrence_type="none",
                recurrence_parent=series,
            )
            # Batch-copy participants (RSVP reset to invited except the owner).
            rows = [
                Participant(
                    event=occurrence,
                    user_id=uid,
                    rsvp=RSVP.ACCEPTED if uid == series.owner_id else RSVP.INVITED,
                )
                for uid in series.participants.values_list("user_id", flat=True)
            ]
            if rows:
                Participant.objects.bulk_create(rows, ignore_conflicts=True)

            emit(
                "calendar.occurrence.materialized",
                {
                    "event_id": str(occurrence.id),
                    "series_id": str(series.id),
                    "scope_key": occurrence.scope_key,
                    "owner_id": str(series.owner_id),
                    "title": occurrence.title,
                    "start": occurrence.start.isoformat(),
                    "end": occurrence.end.isoformat(),
                },
                key=str(series.id),
            )
            from .signals import occurrence_materialized

            occurrence_materialized.send(
                sender=Event, occurrence=occurrence, series=series
            )
    except IntegrityError:
        # Lost the race — a concurrent materialize created the occurrence
        # first (and already emitted). Return its row; do not re-emit.
        winner = _find_occurrence(series, occurrence_start)
        if winner is not None:
            return winner
        raise
    return occurrence


# ── Availability: free/busy + slots ─────────────────────────────────────


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


def free_busy(
    user, range_start: datetime, range_end: datetime, *, scope_key: str | None = None
) -> list[Interval]:
    """Busy intervals for ``user`` in ``[range_start, range_end]``.

    Counts concrete events (standalone + materialized occurrences) the user
    is on with a non-declined RSVP, plus the *virtual* occurrences of every
    series the user is on — minus any occurrence already materialized (which
    is counted as a concrete event, so it is never double-booked).
    """
    concrete_qs = Event.objects.filter(
        participants__user=user,
        participants__rsvp__in=BUSY_RSVP,
        rrule="",
        start__lte=range_end,
        end__gte=range_start,
    )
    series_qs = (
        Event.objects.filter(
            participants__user=user, participants__rsvp__in=BUSY_RSVP
        )
        .exclude(rrule="")
        .distinct()
    )
    if scope_key is not None:
        concrete_qs = concrete_qs.filter(scope_key=scope_key)
        series_qs = series_qs.filter(scope_key=scope_key)

    intervals: list[Interval] = [
        Interval(start=ev.start, end=ev.end) for ev in concrete_qs.distinct()
    ]
    for series in series_qs:
        for occ in expand_event(series, range_start, range_end):
            if occ.is_materialized:
                continue  # already counted as a concrete event
            intervals.append(Interval(start=occ.start, end=occ.end))

    return merge_intervals(intervals)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Sort and coalesce overlapping/adjacent intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: i.start)
    merged = [ordered[0]]
    for cur in ordered[1:]:
        last = merged[-1]
        if cur.start <= last.end:
            if cur.end > last.end:
                merged[-1] = Interval(start=last.start, end=cur.end)
        else:
            merged.append(cur)
    return merged


def subtract_intervals(
    window: Interval, busy: list[Interval]
) -> list[Interval]:
    """Return the parts of ``window`` not covered by ``busy``."""
    free = [window]
    for b in busy:
        next_free: list[Interval] = []
        for f in free:
            if b.end <= f.start or b.start >= f.end:
                next_free.append(f)
                continue
            if b.start > f.start:
                next_free.append(Interval(start=f.start, end=b.start))
            if b.end < f.end:
                next_free.append(Interval(start=b.end, end=f.end))
        free = next_free
    return free


def compute_slots(
    user,
    range_start: datetime,
    range_end: datetime,
    *,
    slot_minutes: int | None = None,
    scope_key: str | None = None,
) -> list[Interval]:
    """Open booking slots for ``user``: working windows in the range minus
    busy intervals, chunked into ``slot_minutes`` blocks."""
    from datetime import date as date_cls
    from zoneinfo import ZoneInfo

    from .conf import calendar_settings
    from .models import AvailabilityWindow

    if slot_minutes is None:
        slot_minutes = calendar_settings.DEFAULT_SLOT_MINUTES
    step = timedelta(minutes=slot_minutes)

    windows_qs = AvailabilityWindow.objects.filter(user=user)
    if scope_key is not None:
        windows_qs = windows_qs.filter(scope_key=scope_key)
    windows = list(windows_qs)
    if not windows:
        return []

    busy = free_busy(user, range_start, range_end, scope_key=scope_key)

    slots: list[Interval] = []
    day = date_cls(range_start.year, range_start.month, range_start.day)
    last_day = date_cls(range_end.year, range_end.month, range_end.day)
    while day <= last_day:
        for w in windows:
            if w.weekday != day.weekday():
                continue
            tz = ZoneInfo(w.timezone)
            win_start = datetime.combine(day, w.start_time, tz)
            win_end = datetime.combine(day, w.end_time, tz)
            win = Interval(
                start=max(win_start, range_start),
                end=min(win_end, range_end),
            )
            if win.start >= win.end:
                continue
            for free in subtract_intervals(win, busy):
                cursor = free.start
                while cursor + step <= free.end:
                    slots.append(Interval(start=cursor, end=cursor + step))
                    cursor += step
        day += timedelta(days=1)
    return slots
