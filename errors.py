"""i18n error keys of stapel-calendar.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_INVALID_RECURRENCE = "error.400.calendar_invalid_recurrence"
ERR_400_INVALID_RSVP = "error.400.calendar_invalid_rsvp"
ERR_400_INVALID_RANGE = "error.400.calendar_invalid_range"
ERR_403_NOT_EVENT_OWNER = "error.403.calendar_not_event_owner"
ERR_404_EVENT_NOT_FOUND = "error.404.calendar_event_not_found"
ERR_404_NOT_INVITED = "error.404.calendar_not_invited"

STAPEL_CALENDAR_ERRORS = {
    ERR_400_INVALID_RECURRENCE: "Invalid recurrence specification",
    ERR_400_INVALID_RSVP: "RSVP must be one of: accepted, tentative, declined",
    ERR_400_INVALID_RANGE: "Invalid time range",
    ERR_403_NOT_EVENT_OWNER: "Only the event owner may perform this action",
    ERR_404_EVENT_NOT_FOUND: "Event not found",
    ERR_404_NOT_INVITED: "You are not invited to this event",
}

register_service_errors(STAPEL_CALENDAR_ERRORS)

__all__ = [
    "STAPEL_CALENDAR_ERRORS",
    "ERR_400_INVALID_RECURRENCE",
    "ERR_400_INVALID_RSVP",
    "ERR_400_INVALID_RANGE",
    "ERR_403_NOT_EVENT_OWNER",
    "ERR_404_EVENT_NOT_FOUND",
    "ERR_404_NOT_INVITED",
]
