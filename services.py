"""Domain services for stapel-calendar.

The generic calendar core: event/participant management, RSVP, recurrence
expansion (virtual) + materialization (on-demand), and availability
(free/busy + slot computation). No app resources are created here — when an
occurrence is materialized the engine *emits* ``calendar.occurrence.materialized``
and the app-layer subscriber owns any resource creation (the coupling this
extraction removes: a ``rooms.Room`` used to be created directly inside the
recurrence loop).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from stapel_core.comm import emit

from .models import BUSY_RSVP, Event, EventStatus, Participant, RSVP
from .recurrence import (
    Expansion,
    InvalidRecurrence,
    Occurrence,
    add_duration,
    as_utc,
    build_rrule,
    expand_rule_detailed,
    is_rule_instant,
)


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
    accepted participant and any invitees in one batch.

    ``end < start`` is rejected. ``end == start`` (zero duration) is allowed
    — a marker/deadline-style event; it occupies no time in free/busy and
    never blocks a slot.
    """
    if end < start:
        raise ValueError("event end must not be before start")

    rrule_text = build_rrule(
        recurrence_type,
        interval=recurrence_interval,
        byweekday=recurrence_weekdays,
        until=recurrence_until,
        count=recurrence_count,
        dtstart=start,
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
    """Batch-create participants: owner accepted, the rest invited (replaces
    per-loop ``create`` calls)."""
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


def replace_participants(event: Event, participant_ids) -> Event:
    """Replace ``event``'s participant set — the PUT replace-set semantics.

    The resulting set is exactly ``{owner} ∪ participant_ids``: the owner is
    always retained as ``ACCEPTED`` (an event cannot exist without its owner,
    so passing a list that omits the owner does not remove them); ids already
    on the event **keep their existing RSVP** (a re-sent list must not silently
    reset an accepted invite back to invited); brand-new ids are added as
    ``INVITED``; ids no longer in the list are removed. Idempotent.
    """
    owner_id = str(event.owner_id)
    desired = {owner_id} | {str(uid) for uid in (participant_ids or [])}
    existing = {str(p.user_id): p for p in event.participants.all()}

    stale_ids = [p.id for uid, p in existing.items() if uid not in desired]
    if stale_ids:
        Participant.objects.filter(id__in=stale_ids).delete()

    new_rows = [
        Participant(
            event=event,
            user_id=uid,
            rsvp=RSVP.ACCEPTED if uid == owner_id else RSVP.INVITED,
        )
        for uid in desired
        if uid not in existing
    ]
    if new_rows:
        Participant.objects.bulk_create(new_rows, ignore_conflicts=True)
    return event


#: Fields whose change on a series master re-derives its RRULE and forces a
#: series rebuild (``start`` is the DTSTART anchor — moving it shifts every
#: rule instant). See :func:`update_event`.
_RRULE_INPUT_FIELDS = frozenset(
    {
        "recurrence_type",
        "recurrence_interval",
        "recurrence_weekdays",
        "recurrence_until",
        "recurrence_count",
    }
)

_SIMPLE_FIELDS = ("title", "description", "start", "end", "status")


def update_event(event: Event, changes: dict) -> Event:
    """Partially update ``event`` (the PATCH surface) and, for a series master
    whose rule changed, rebuild the series.

    ``changes`` carries only the fields the client actually sent (partial
    semantics). Plain fields (``title``/``description``/``start``/``end``/
    ``status``) are assigned as given. ``end < start`` (whether from the new
    values or one new + one existing) is rejected with ``ValueError``.

    **Series rebuild.** When ``event`` is a series master and the request
    touches any recurrence input (``recurrence_type`` / ``recurrence_interval``
    / ``recurrence_weekdays`` / ``recurrence_until`` / ``recurrence_count``) or
    ``start`` (the DTSTART anchor), the canonical RRULE is rebuilt from the
    supplied recurrence spec — symmetrically to :func:`create_event`, and with
    the same validation (:class:`~.recurrence.InvalidRecurrence`). Because the
    library stores only the canonical RRULE (never its constituent inputs), a
    recurrence edit **re-specifies the whole rule**: unspecified recurrence
    params fall back to ``None``/unset exactly as at create time — send the
    complete spec, do not expect a partial merge into the stored RRULE.

    **Fate of materialized occurrence exceptions.** The only per-instant state
    the engine persists is materialized occurrence child rows (a reschedule, an
    RSVP, an attached app resource, or a CANCELLED tombstone — the EXDATE
    analog). After a rebuild each child is reconciled against the NEW rule by
    its ``recurrence_id`` (the original rule instant it stands for):

    - the instant is **still** an instant of the new rule → the child is kept
      untouched (its reschedule/RSVPs/resource/tombstone survive);
    - the instant is **gone** and the child is a CANCELLED tombstone → it is
      **deleted** (an EXDATE for an instant that no longer exists is
      meaningless);
    - the instant is **gone** and the child carries real state → it is
      **detached into a standalone event** (``recurrence_parent`` and
      ``recurrence_id`` cleared): a rule edit never silently destroys a user's
      RSVPs or an app's attached resource — the occurrence simply survives as
      an independent event at its own start/end.

    Turning recurrence off (``recurrence_type="none"``) rebuilds to an empty
    RRULE: the master becomes a standalone event and every former child is
    orphaned by the same rules above.
    """
    touches_rule = bool(changes) and (
        event.is_series_master
        and (
            "start" in changes
            or any(f in changes for f in _RRULE_INPUT_FIELDS)
        )
    )

    for name in _SIMPLE_FIELDS:
        if name in changes:
            setattr(event, name, changes[name])
    if event.end < event.start:
        raise ValueError("event end must not be before start")

    update_fields = [f for f in _SIMPLE_FIELDS if f in changes]

    if touches_rule:
        recurrence_type = changes.get("recurrence_type", event.recurrence_type)
        event.rrule = build_rrule(
            recurrence_type,
            interval=changes.get("recurrence_interval"),
            byweekday=changes.get("recurrence_weekdays") or None,
            until=changes.get("recurrence_until"),
            count=changes.get("recurrence_count"),
            dtstart=event.start,
        )
        event.recurrence_type = recurrence_type
        update_fields += ["rrule", "recurrence_type"]

    if update_fields:
        # updated_at is auto_now; include it so the mtime advances.
        event.save(update_fields=sorted(set(update_fields)) + ["updated_at"])

    if touches_rule:
        _reconcile_occurrences(event)
    return event


def _reconcile_occurrences(master: Event) -> None:
    """Reconcile a rebuilt master's materialized children against its new rule.

    See :func:`update_event` for the semantics: keep children on still-valid
    instants, delete orphaned tombstones, detach orphaned real occurrences into
    standalone events.
    """
    for child in master.occurrences.all():
        instant = child.recurrence_id or child.start
        still_valid = bool(master.rrule) and is_rule_instant(
            master.rrule, master.start, instant
        )
        if still_valid:
            continue
        if child.status == EventStatus.CANCELLED:
            child.delete()
            continue
        child.recurrence_parent = None
        child.recurrence_id = None
        child.save(update_fields=["recurrence_parent", "recurrence_id", "updated_at"])


# ── Recurrence: virtual expansion + on-demand materialization ───────────


def expand_event_detailed(
    event: Event, range_start: datetime, range_end: datetime
) -> Expansion:
    """Virtually expand a series master over a range.

    Occurrences already materialized (a child row exists for that *rule
    instant* — ``recurrence_id``) are flagged, carry their concrete id and
    report the concrete row's actual start/end (which may differ from the
    rule instant if the occurrence was rescheduled); the rest are virtual
    and never persisted. Materialized rows with ``status=CANCELLED`` are
    tombstones (the EXDATE analog): their instant is omitted entirely. A
    cancelled master (or standalone event) expands to nothing.

    Instants are matched in UTC space (:func:`~.recurrence.as_utc`) — raw
    datetime dict keys miss on DST gap/fold wall times per PEP 495.
    """
    if event.status == EventStatus.CANCELLED:
        return Expansion()
    if not event.rrule:
        # A concrete/standalone event contributes itself if it intersects.
        if event.start <= range_end and event.end >= range_start:
            return Expansion(
                occurrences=[
                    Occurrence(
                        event_id=event.id,
                        start=event.start,
                        end=event.end,
                        is_materialized=True,
                        materialized_id=event.id,
                    )
                ]
            )
        return Expansion()

    materialized = {
        as_utc(row.recurrence_id or row.start): row
        for row in event.occurrences.all().only(
            "id", "start", "end", "status", "recurrence_id"
        )
    }
    expansion = expand_rule_detailed(
        event.rrule,
        event.start,
        event.duration,
        range_start,
        range_end,
        event_id=event.id,
    )
    out: list[Occurrence] = []
    for occ in expansion.occurrences:
        row = materialized.get(as_utc(occ.start))
        if row is None:
            out.append(occ)
            continue
        if row.status == EventStatus.CANCELLED:
            continue  # tombstone — the EXDATE analog
        out.append(
            Occurrence(
                event_id=event.id,
                start=row.start,
                end=row.end,
                is_materialized=True,
                materialized_id=row.id,
            )
        )
    return Expansion(occurrences=out, truncated=expansion.truncated)


def expand_event(
    event: Event, range_start: datetime, range_end: datetime
) -> list[Occurrence]:
    """List-only variant of :func:`expand_event_detailed` (drops the
    ``truncated`` flag)."""
    return expand_event_detailed(event, range_start, range_end).occurrences


def _find_occurrence(series: Event, occurrence_start: datetime):
    return series.occurrences.filter(recurrence_id=occurrence_start).first()


def materialize(
    series: Event, occurrence_start: datetime, *, off_rule: bool = False
) -> Event:
    """Persist a single occurrence of ``series`` so it can gain its own state
    (RSVP, resource). Idempotent: a second call for the same instant returns
    the existing row. Emits ``calendar.occurrence.materialized`` (and the
    ``occurrence_materialized`` Django signal) — this engine creates **no**
    app resource itself.

    ``occurrence_start`` must be an actual instant of the series' rule
    (within its UNTIL/COUNT bounds) — otherwise the row would be a ghost:
    invisible to expansion yet still counted busy. Pass ``off_rule=True`` to
    deliberately create an exception occurrence outside the rule. Raises
    :class:`~.recurrence.InvalidRecurrence` for off-rule instants.

    The row records ``recurrence_id = occurrence_start`` (RFC 5545
    RECURRENCE-ID analog): rescheduling the row moves ``start``/``end`` but
    keeps claiming the original rule instant, and cancelling it
    (``status=CANCELLED``) tombstones the instant — see
    :func:`cancel_occurrence`. The end is computed in instant space
    (:func:`~.recurrence.add_duration`), so a DST-transition occurrence
    keeps the series' exact duration instead of persisting an inverted or
    inflated interval.

    Concurrency-safe: the ``(recurrence_parent, recurrence_id)`` partial
    unique constraint guarantees at most one occurrence per instant. Two
    concurrent materialize calls for the same slot (the normal
    concurrent-booking case) both see no existing row, race to ``create()``,
    and the loser catches the ``IntegrityError``, re-queries and returns the
    winner's row — **without** re-emitting the event or re-sending the
    signal (the winner already did).
    """
    if not series.is_series_master:
        raise ValueError("can only materialize occurrences of a series master")
    existing = _find_occurrence(series, occurrence_start)
    if existing is not None:
        return existing
    if not off_rule and not is_rule_instant(
        series.rrule, series.start, occurrence_start
    ):
        raise InvalidRecurrence(
            f"{occurrence_start.isoformat()} is not an instant of the series "
            "rule (pass off_rule=True to materialize an exception occurrence)"
        )

    return _persist_occurrence(
        series, occurrence_start, status=series.status, notify=True
    )


def _persist_occurrence(
    series: Event, occurrence_start: datetime, *, status: str, notify: bool
) -> Event:
    """Shared create path for materialize/cancel: idempotent, race-safe.
    ``notify=False`` suppresses the materialized hook (tombstones must not
    trigger app-layer resource creation)."""
    existing = _find_occurrence(series, occurrence_start)
    if existing is not None:
        return existing

    occurrence_end = add_duration(occurrence_start, series.duration)
    # Compare as instants: intra-zone comparison is wall-clock and would
    # call a fold-crossing end (e.g. 01:00 EST after a 01:30 EDT start)
    # "before" its start.
    if as_utc(occurrence_end) < as_utc(occurrence_start):
        raise ValueError("occurrence end before start (negative series duration)")
    try:
        with transaction.atomic():
            occurrence = Event.objects.create(
                owner=series.owner,
                title=series.title,
                description=series.description,
                start=occurrence_start,
                end=occurrence_end,
                scope_key=series.scope_key,
                status=status,
                rrule="",
                recurrence_type="none",
                recurrence_parent=series,
                recurrence_id=occurrence_start,
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

            if notify:
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


def cancel_occurrence(series: Event, occurrence_start: datetime) -> Event:
    """Cancel a single occurrence of a series — the EXDATE analog.

    Materializes a *tombstone* if no row exists yet (without emitting
    ``calendar.occurrence.materialized`` — a cancelled instant must not
    trigger app-layer resource creation) or flips an existing row to
    ``status=CANCELLED``. Expansion and free/busy then skip both the
    concrete row and the virtual occurrence at its ``recurrence_id``, so
    the instant cannot silently "resurrect". The ``DELETE /events/{id}``
    API on a materialized occurrence does the same (it tombstones instead
    of deleting the row). Idempotent."""
    if not series.is_series_master:
        raise ValueError("can only cancel occurrences of a series master")
    occurrence = _find_occurrence(series, occurrence_start)
    if occurrence is None:
        if not is_rule_instant(series.rrule, series.start, occurrence_start):
            raise InvalidRecurrence(
                f"{occurrence_start.isoformat()} is not an instant of the "
                "series rule"
            )
        occurrence = _persist_occurrence(
            series, occurrence_start, status=EventStatus.CANCELLED, notify=False
        )
    if occurrence.status != EventStatus.CANCELLED:
        occurrence.status = EventStatus.CANCELLED
        occurrence.save(update_fields=["status", "updated_at"])
    return occurrence


# ── Availability: free/busy + slots ─────────────────────────────────────


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class FreeBusyResult:
    """Busy intervals plus a ``truncated`` flag: True when at least one
    series expansion hit the ``MAX_EXPANSION_OCCURRENCES`` cap inside the
    range — everything past the cap merely *looks* free, so booking logic
    must not treat the tail of the range as available."""

    busy: list[Interval] = field(default_factory=list)
    truncated: bool = False


def free_busy_detailed(
    user, range_start: datetime, range_end: datetime, *, scope_key: str | None = None
) -> FreeBusyResult:
    """Busy intervals for ``user`` in ``[range_start, range_end]``.

    Counts concrete events (standalone + materialized occurrences) the user
    is on with a non-declined RSVP, plus the *virtual* occurrences of every
    series the user is on — minus any occurrence already materialized (which
    is counted as a concrete event, so it is never double-booked).
    ``status=CANCELLED`` events (tombstoned occurrences, cancelled events
    and whole cancelled series) contribute nothing. Intervals are clipped
    to the requested range.
    """
    concrete_qs = Event.objects.filter(
        participants__user=user,
        participants__rsvp__in=BUSY_RSVP,
        rrule="",
        start__lte=range_end,
        end__gte=range_start,
    ).exclude(status=EventStatus.CANCELLED)
    series_qs = (
        Event.objects.filter(
            participants__user=user, participants__rsvp__in=BUSY_RSVP
        )
        .exclude(rrule="")
        .exclude(status=EventStatus.CANCELLED)
        .distinct()
    )
    if scope_key is not None:
        concrete_qs = concrete_qs.filter(scope_key=scope_key)
        series_qs = series_qs.filter(scope_key=scope_key)

    intervals: list[Interval] = [
        Interval(start=ev.start, end=ev.end) for ev in concrete_qs.distinct()
    ]
    truncated = False
    for series in series_qs:
        expansion = expand_event_detailed(series, range_start, range_end)
        truncated = truncated or expansion.truncated
        for occ in expansion.occurrences:
            if occ.is_materialized:
                continue  # already counted as a concrete event
            intervals.append(Interval(start=occ.start, end=occ.end))

    clipped = [
        Interval(start=max(i.start, range_start), end=min(i.end, range_end))
        for i in intervals
    ]
    return FreeBusyResult(busy=merge_intervals(clipped), truncated=truncated)


def free_busy(
    user, range_start: datetime, range_end: datetime, *, scope_key: str | None = None
) -> list[Interval]:
    """List-only variant of :func:`free_busy_detailed` (drops the
    ``truncated`` flag)."""
    return free_busy_detailed(
        user, range_start, range_end, scope_key=scope_key
    ).busy


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Sort and coalesce overlapping/adjacent intervals.

    Degenerate intervals (``end <= start``) are dropped: a zero-length
    interval covers no time (so it must not split free windows or shift the
    slot grid), and an inverted one is invalid input — subtracting it would
    produce overlapping "free" intervals, so it is defensively discarded
    here rather than propagated into availability math."""
    ordered = sorted(
        (i for i in intervals if i.end > i.start), key=lambda i: i.start
    )
    if not ordered:
        return []
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


@dataclass(frozen=True)
class SlotsResult:
    """Open slots plus the ``truncated`` flag of the underlying free/busy
    expansion (see :class:`FreeBusyResult`) — when True, slots past the
    expansion cap may collide with uncomputed occurrences."""

    slots: list[Interval] = field(default_factory=list)
    truncated: bool = False


def compute_slots_detailed(
    user,
    range_start: datetime,
    range_end: datetime,
    *,
    slot_minutes: int | None = None,
    scope_key: str | None = None,
) -> SlotsResult:
    """Open booking slots for ``user``: working windows in the range minus
    busy intervals, chunked into ``slot_minutes`` blocks.

    ``slot_minutes`` must be >= 1 — zero or negative steps are rejected
    (``ValueError``); a non-positive step would loop forever.

    Days are iterated in each *window's* timezone: the window's weekday and
    wall-clock times are local to ``AvailabilityWindow.timezone``, so the
    day cursor must cover every local date the UTC range touches — deriving
    it from the range's timezone drops a window on the edge of the range
    for zones ahead of/behind it (e.g. a Pacific/Auckland Monday-morning
    window whose UTC image is still Sunday).
    """
    from zoneinfo import ZoneInfo

    from .conf import calendar_settings
    from .models import AvailabilityWindow

    if slot_minutes is None:
        slot_minutes = calendar_settings.DEFAULT_SLOT_MINUTES
    slot_minutes = int(slot_minutes)
    if slot_minutes < 1:
        raise ValueError("slot_minutes must be >= 1")
    step = timedelta(minutes=slot_minutes)

    windows_qs = AvailabilityWindow.objects.filter(user=user)
    if scope_key is not None:
        windows_qs = windows_qs.filter(scope_key=scope_key)
    windows = list(windows_qs)
    if not windows:
        return SlotsResult()

    result = free_busy_detailed(user, range_start, range_end, scope_key=scope_key)
    busy = result.busy

    slots: list[Interval] = []
    for w in windows:
        tz = ZoneInfo(w.timezone)
        day = (range_start.astimezone(tz) if range_start.tzinfo else range_start).date()
        last_day = (range_end.astimezone(tz) if range_end.tzinfo else range_end).date()
        while day <= last_day:
            if day.weekday() != w.weekday:
                day += timedelta(days=1)
                continue
            win_start = datetime.combine(day, w.start_time, tz)
            win_end = datetime.combine(day, w.end_time, tz)
            win = Interval(
                start=max(win_start, range_start),
                end=min(win_end, range_end),
            )
            if win.start >= win.end:
                day += timedelta(days=1)
                continue
            for free in subtract_intervals(win, busy):
                cursor = free.start
                while cursor + step <= free.end:
                    slots.append(Interval(start=cursor, end=cursor + step))
                    cursor += step
            day += timedelta(days=1)
    slots.sort(key=lambda s: (s.start, s.end))
    return SlotsResult(slots=slots, truncated=result.truncated)


def compute_slots(
    user,
    range_start: datetime,
    range_end: datetime,
    *,
    slot_minutes: int | None = None,
    scope_key: str | None = None,
) -> list[Interval]:
    """List-only variant of :func:`compute_slots_detailed` (drops the
    ``truncated`` flag)."""
    return compute_slots_detailed(
        user,
        range_start,
        range_end,
        slot_minutes=slot_minutes,
        scope_key=scope_key,
    ).slots
