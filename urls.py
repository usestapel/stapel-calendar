"""URL patterns — no global prefix here, the host project mounts them:

    path("calendar/", include("stapel_calendar.urls"))
"""
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
