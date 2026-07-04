"""Models for stapel-calendar.

The generic calendar core: ``Event`` (a scheduled thing — a meeting, a
booking, an appointment), ``Participant`` + RSVP, and ``AvailabilityWindow``
(working hours, the booking primitive).

House rules (docs/library-standard.md §3.8):
- cross-service references are UUID fields, not FKs (Event.id is a UUID so
  the ``calendar.occurrence.materialized`` event id is a stable
  cross-service handle the host can pin a resource to);
- the user model is only ``settings.AUTH_USER_MODEL``;
- **no FK to Organization/Room** — scoping is the opaque ``scope_key``
  string and the *resource* is an app-layer concern reached via a comm emit.
"""
import uuid

from django.conf import settings
from django.db import models


class RecurrenceType(models.TextChoices):
    """Human-facing recurrence presets (map to RFC 5545 RRULE at write time).

    Members:
        NONE: One-off event, no recurrence.
        DAILY: Every day.
        WEEKDAYS: Every Mon-Fri.
        WEEKLY: Same weekday every week.
        BIWEEKLY: Same weekday every two weeks.
        MONTHLY: Same day-of-month every month (RRULE skips short months).
        CUSTOM: Explicit set of weekdays (BYDAY).
    """

    NONE = "none", "None"
    DAILY = "daily", "Daily"
    WEEKDAYS = "weekdays", "Weekdays"
    WEEKLY = "weekly", "Weekly"
    BIWEEKLY = "biweekly", "Biweekly"
    MONTHLY = "monthly", "Monthly"
    CUSTOM = "custom", "Custom"


class EventStatus(models.TextChoices):
    CONFIRMED = "confirmed", "Confirmed"
    TENTATIVE = "tentative", "Tentative"
    CANCELLED = "cancelled", "Cancelled"


class RSVP(models.TextChoices):
    INVITED = "invited", "Invited"
    ACCEPTED = "accepted", "Accepted"
    TENTATIVE = "tentative", "Tentative"
    DECLINED = "declined", "Declined"


# RSVP states that count as "occupying" the participant's calendar for
# free/busy purposes (a declined invite does not).
BUSY_RSVP = (RSVP.INVITED, RSVP.ACCEPTED, RSVP.TENTATIVE)


class Event(models.Model):
    """A scheduled event — meeting, booking or appointment.

    Recurrence is stored as a canonical RFC 5545 RRULE string on the *series
    master* (``rrule`` non-empty, ``recurrence_parent`` null). A concrete
    persisted occurrence is a child row (``rrule`` empty,
    ``recurrence_parent`` set) — created on-demand only when the occurrence
    gains its own state (an RSVP or a host resource). Occurrences that carry
    no own state stay *virtual* (computed from the RRULE, never persisted).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")

    start = models.DateTimeField()
    end = models.DateTimeField()

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_events",
    )
    # Opaque host-supplied scope (workspace_id / org_id / tenant / ""). The
    # library never interprets it; the SCOPE_PROVIDER seam resolves & filters.
    scope_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    status = models.CharField(
        max_length=16, choices=EventStatus.choices, default=EventStatus.CONFIRMED
    )

    # Canonical recurrence spec: an RFC 5545 RRULE line WITHOUT DTSTART
    # (e.g. "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE"). Empty = non-recurring.
    rrule = models.TextField(blank=True, default="")
    # Convenience mirror of the preset used to build `rrule` (display only;
    # `rrule` is the source of truth).
    recurrence_type = models.CharField(
        max_length=20, choices=RecurrenceType.choices, default=RecurrenceType.NONE
    )
    # Series -> occurrence link. A materialized occurrence points back here.
    recurrence_parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="occurrences",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start"]
        indexes = [
            models.Index(fields=["owner", "start"], name="cal_event_owner_start"),
            models.Index(fields=["scope_key", "start"], name="cal_event_scope_start"),
            models.Index(fields=["recurrence_parent"], name="cal_event_parent"),
        ]
        constraints = [
            # A materialized occurrence must not itself carry an RRULE, and a
            # given series can hold at most one materialized child per start.
            models.UniqueConstraint(
                fields=["recurrence_parent", "start"],
                condition=models.Q(recurrence_parent__isnull=False),
                name="cal_event_uniq_occurrence",
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.start:%Y-%m-%d %H:%M})"

    @property
    def duration(self):
        return self.end - self.start

    @property
    def is_series_master(self) -> bool:
        return bool(self.rrule) and self.recurrence_parent_id is None

    @property
    def is_occurrence(self) -> bool:
        return self.recurrence_parent_id is not None


class Participant(models.Model):
    """A user invited to an event, with their RSVP.

    Reminder delivery is **event-driven** (a ``calendar.event.reminder_due``
    emit) — there is deliberately no ``notified`` boolean here; dedup and
    delivery are the notifications module's concern.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="event_participations",
    )
    rsvp = models.CharField(max_length=10, choices=RSVP.choices, default=RSVP.INVITED)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"], name="cal_participant_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["user"], name="cal_participant_user"),
        ]

    def __str__(self):
        return f"{self.user_id} @ {self.event_id} ({self.rsvp})"


class AvailabilityWindow(models.Model):
    """A recurring working window for a user (the booking availability
    primitive): "available for booking on weekday W from start_time to
    end_time in this timezone". Free/busy subtracts busy events from these
    windows to compute open slots.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="availability_windows",
    )
    scope_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # 0 = Monday ... 6 = Sunday (matches datetime.weekday()).
    weekday = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    # IANA timezone name the window's wall-clock times are expressed in.
    timezone = models.CharField(max_length=64, default="UTC")

    class Meta:
        ordering = ["weekday", "start_time"]
        indexes = [
            models.Index(fields=["user", "weekday"], name="cal_avail_user_weekday"),
        ]

    def __str__(self):
        return f"{self.user_id} wd={self.weekday} {self.start_time}-{self.end_time}"
