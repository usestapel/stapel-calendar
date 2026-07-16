"""v1 URL set for stapel-calendar (api-versioning.md §2, §6).

No global prefix here — the root ``urls.py`` mounts this module under
``api/v1/`` and the host mounts that under ``calendar/``:

    path("calendar/", include("stapel_calendar.urls"))   # -> /calendar/api/v1/...
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    AvailabilityView,
    CalendarView,
    EventDetailView,
    EventICSView,
    EventListCreateView,
    EventParticipantsView,
    EventRespondView,
)

urlpatterns = [
    path("events", EventListCreateView.as_view(), name="calendar-events"),
    path(
        "events/<uuid:event_id>",
        EventDetailView.as_view(),
        name="calendar-event-detail",
    ),
    path(
        "events/<uuid:event_id>/participants",
        EventParticipantsView.as_view(),
        name="calendar-event-participants",
    ),
    path(
        "events/<uuid:event_id>/respond",
        EventRespondView.as_view(),
        name="calendar-event-respond",
    ),
    path(
        "events/<uuid:event_id>/ics",
        EventICSView.as_view(),
        name="calendar-event-ics",
    ),
    path("calendar", CalendarView.as_view(), name="calendar-user-calendar"),
    path("availability", AvailabilityView.as_view(), name="calendar-availability"),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns (capability-config.md §2 p.2).

    ``flags`` compose with OR — the block is mounted while ANY flag is on,
    and disappears only when ALL of them are off. Empty flags = always on.
    """
    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2): calendar has no per-method
#: config gates (its settings are tuning knobs and dotted-path seams, none
#: unmounts endpoints) — the whole URL surface is a single always-on block.
#: Declared as a registry entry (rather than left implicit) so the
#: capabilities.json emitter has a uniform mechanism across every module.
GATE_REGISTRY: dict = {
    'calendar.api': GateEntry('calendar.api', (), tuple(urlpatterns)),
}
