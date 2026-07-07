"""Reminder policy — extension seam #2.

Replaces a dead ``notified`` boolean (and the absent scheduler) with an
**event-driven** design: a host cron calls :func:`run_reminders`
on an interval; the configured ``ReminderPolicy`` decides which reminders
are due and emits ``calendar.event.reminder_due`` for stapel-notifications
(or any subscriber) to deliver. Dedup is the consumer's job — every emit
carries a stable ``dedup_key`` (``"<event_id>:<offset>"``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from stapel_core.comm import emit


@dataclass(frozen=True)
class DueReminder:
    """A reminder the policy decided to fire for an event occurrence."""

    event_id: object
    offset_minutes: int
    fire_at: datetime


class ReminderPolicy:
    """Contract for the reminder seam. Override ``due_for`` to change the
    cadence, and/or ``emit_for`` to change the payload/channel."""

    def due_for(self, event, now: datetime, window: timedelta) -> list[DueReminder]:
        """Return the reminders for ``event`` whose fire time falls in
        ``[now, now + window)``."""
        raise NotImplementedError

    def emit_for(self, event, reminder: DueReminder) -> None:
        """Emit/deliver a single due reminder."""
        raise NotImplementedError


class DefaultReminderPolicy(ReminderPolicy):
    """Fire one ``calendar.event.reminder_due`` per configured offset, once
    the offset's fire time (``start - offset``) enters the scan window."""

    @property
    def offsets(self) -> list[int]:
        from .conf import calendar_settings

        return list(calendar_settings.REMINDER_OFFSETS or [])

    def due_for(self, event, now: datetime, window: timedelta) -> list[DueReminder]:
        out: list[DueReminder] = []
        for offset in self.offsets:
            fire_at = event.start - timedelta(minutes=offset)
            if now <= fire_at < now + window:
                out.append(
                    DueReminder(
                        event_id=event.id, offset_minutes=offset, fire_at=fire_at
                    )
                )
        return out

    def emit_for(self, event, reminder: DueReminder) -> None:
        participant_ids = [
            str(uid)
            for uid in event.participants.values_list("user_id", flat=True)
        ]
        emit(
            "calendar.event.reminder_due",
            {
                "event_id": str(event.id),
                "scope_key": event.scope_key,
                "owner_id": str(event.owner_id),
                "title": event.title,
                "start": event.start.isoformat(),
                "offset_minutes": reminder.offset_minutes,
                "participant_ids": participant_ids,
                "dedup_key": f"{event.id}:{reminder.offset_minutes}",
            },
            key=str(event.id),
        )


def get_reminder_policy() -> ReminderPolicy:
    from .conf import calendar_settings

    policy = calendar_settings.REMINDER_POLICY
    return policy() if isinstance(policy, type) else policy


def run_reminders(now: datetime | None = None) -> int:
    """Scan upcoming events and emit due reminders. Returns the number of
    reminders emitted. Intended to be called by a host cron every
    ``REMINDER_SCAN_WINDOW_SECONDS``.

    Only concrete events are scanned (non-recurring standalones and
    materialized occurrences) — a virtual occurrence has no own state to
    remind against until it is materialized.
    """
    from django.utils import timezone

    from .conf import calendar_settings
    from .models import Event, EventStatus

    if now is None:
        now = timezone.now()
    window = timedelta(seconds=calendar_settings.REMINDER_SCAN_WINDOW_SECONDS)
    policy = get_reminder_policy()

    # Candidate lookahead: the largest configured offset bounds how far ahead
    # an event's start can be while still having a reminder due now. This is a
    # coarse pre-filter (the policy's due_for makes the final call), sized off
    # the REMINDER_OFFSETS setting rather than any policy internal so a custom
    # policy is still fed the right candidates.
    max_offset = max(calendar_settings.REMINDER_OFFSETS or [0], default=0)
    horizon = now + timedelta(minutes=max_offset) + window
    candidates = (
        Event.objects.filter(rrule="", start__gte=now, start__lte=horizon)
        .exclude(status=EventStatus.CANCELLED)
        .prefetch_related("participants")
    )
    emitted = 0
    for event in candidates:
        for reminder in policy.due_for(event, now, window):
            policy.emit_for(event, reminder)
            emitted += 1
    return emitted
