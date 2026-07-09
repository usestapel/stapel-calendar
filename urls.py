"""URL patterns — no global prefix here, the host project mounts them:

    path("calendar/", include("stapel_calendar.urls"))
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    AvailabilityView,
    CalendarView,
    EventDetailView,
    EventICSView,
    EventListCreateView,
    EventRespondView,
)

urlpatterns = [
    path("api/events", EventListCreateView.as_view(), name="calendar-events"),
    path(
        "api/events/<uuid:event_id>",
        EventDetailView.as_view(),
        name="calendar-event-detail",
    ),
    path(
        "api/events/<uuid:event_id>/respond",
        EventRespondView.as_view(),
        name="calendar-event-respond",
    ),
    path(
        "api/events/<uuid:event_id>/ics",
        EventICSView.as_view(),
        name="calendar-event-ics",
    ),
    path("api/calendar", CalendarView.as_view(), name="calendar-user-calendar"),
    path("api/availability", AvailabilityView.as_view(), name="calendar-availability"),
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
