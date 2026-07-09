"""Dataclass DTOs — the API models of stapel-calendar (never ORM instances)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class ParticipantResponse:
    """An invitee and their RSVP.

    Attributes:
        user_id: The participant's user id.
        rsvp: One of invited/accepted/tentative/declined.
    """

    user_id: str
    rsvp: str


@dataclass
class EventResponse:
    """A calendar event (series master or concrete occurrence).

    Attributes:
        id: Event id (UUID).
        title: Event title.
        description: Free-text description.
        start: Start (tz-aware ISO 8601).
        end: End (tz-aware ISO 8601).
        owner_id: Creator's user id.
        scope_key: Opaque host scope (workspace/org/tenant).
        status: confirmed/tentative/cancelled.
        recurrence_type: Human preset used to build the RRULE.
        rrule: Canonical RFC 5545 RRULE line (empty for non-recurring).
        recurrence_parent_id: Series master id if this is a materialized occurrence.
        participants: Invitees with RSVPs.
    """

    id: str
    title: str
    description: str
    start: datetime
    end: datetime
    owner_id: str
    scope_key: str
    status: str
    recurrence_type: str
    rrule: str
    recurrence_parent_id: Optional[str] = None
    participants: List[ParticipantResponse] = field(default_factory=list)


@dataclass
class OccurrenceResponse:
    """A single (possibly virtual) instance of a recurring event.

    Attributes:
        event_id: The series master id.
        start: Occurrence start.
        end: Occurrence end.
        is_materialized: True if a concrete row exists for this instant.
        materialized_id: The concrete occurrence id, if materialized.
    """

    event_id: str
    start: datetime
    end: datetime
    is_materialized: bool
    materialized_id: Optional[str] = None


@dataclass
class CalendarResponse:
    """A user's calendar over a range: concrete events + expanded occurrences.

    Attributes:
        events: Concrete/standalone events overlapping the range.
        occurrences: Expanded (virtual + materialized) occurrences of series.
    """

    events: List[EventResponse] = field(default_factory=list)
    occurrences: List[OccurrenceResponse] = field(default_factory=list)


@dataclass
class IntervalResponse:
    """A time interval.

    Attributes:
        start: Interval start.
        end: Interval end.
    """

    start: datetime
    end: datetime


@dataclass
class AvailabilityResponse:
    """Free/busy + open slots for a user.

    Attributes:
        busy: Coalesced busy intervals in the range.
        slots: Open booking slots (empty if no availability windows set).
        truncated: True when a series expansion hit the
            MAX_EXPANSION_OCCURRENCES cap inside the range — times past the
            cap only look free; don't book them blindly.
    """

    busy: List[IntervalResponse] = field(default_factory=list)
    slots: List[IntervalResponse] = field(default_factory=list)
    truncated: bool = False


# ── Request DTOs ────────────────────────────────────────────────────────


@dataclass
class EventCreateRequest:
    """Create an event.

    Attributes:
        title: Event title.
        start: Start (tz-aware ISO 8601).
        end: End (tz-aware ISO 8601).
        description: Optional description.
        recurrence_type: none/daily/weekdays/weekly/biweekly/monthly/custom.
        recurrence_interval: RRULE INTERVAL (>=1).
        recurrence_weekdays: Weekday ints (0=Mon..6=Sun) for custom.
        recurrence_until: Series end (tz-aware ISO 8601), maps to RRULE UNTIL.
        recurrence_count: Number of occurrences, maps to RRULE COUNT.
        participant_ids: User ids to invite.
    """

    title: str
    start: datetime
    end: datetime
    description: str = ""
    recurrence_type: str = "none"
    recurrence_interval: Optional[int] = None
    recurrence_weekdays: List[int] = field(default_factory=list)
    recurrence_until: Optional[datetime] = None
    recurrence_count: Optional[int] = None
    participant_ids: List[str] = field(default_factory=list)


@dataclass
class RSVPRequest:
    """Respond to an event invitation.

    Attributes:
        rsvp: One of accepted/tentative/declined.
    """

    rsvp: str


@dataclass
class EventUpdateRequest:
    """Partially update an event (PATCH). Every field is optional — only the
    fields present in the request body are changed.

    Editing any recurrence field (or ``start``, the series anchor) of a series
    master re-specifies and rebuilds the whole rule — send the COMPLETE
    recurrence spec, exactly as for create (the library stores only the
    canonical RRULE, not its constituent inputs, so recurrence params cannot be
    merged individually).

    Attributes:
        title: New title.
        description: New description.
        start: New start (tz-aware ISO 8601).
        end: New end (tz-aware ISO 8601).
        status: confirmed/tentative/cancelled.
        recurrence_type: none/daily/weekdays/weekly/biweekly/monthly/custom.
        recurrence_interval: RRULE INTERVAL (>=1).
        recurrence_weekdays: Weekday ints (0=Mon..6=Sun) for custom.
        recurrence_until: Series end (tz-aware ISO 8601), maps to RRULE UNTIL.
        recurrence_count: Number of occurrences, maps to RRULE COUNT.
    """

    title: Optional[str] = None
    description: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    status: Optional[str] = None
    recurrence_type: Optional[str] = None
    recurrence_interval: Optional[int] = None
    recurrence_weekdays: List[int] = field(default_factory=list)
    recurrence_until: Optional[datetime] = None
    recurrence_count: Optional[int] = None


@dataclass
class ParticipantsReplaceRequest:
    """Replace an event's participant set (PUT).

    Attributes:
        participant_ids: The complete desired invitee list. The owner is always
            retained (as accepted) whether or not present; ids already on the
            event keep their RSVP; new ids are added as invited; absent ids are
            removed.
    """

    participant_ids: List[str] = field(default_factory=list)
