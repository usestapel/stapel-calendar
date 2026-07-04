"""GDPR data handler for stapel-calendar.

This module holds user PII: ``Event.owner``, ``Participant.user`` and
``AvailabilityWindow.user``. Per the Stapel standard, a data-holding module
subscribes to ``user.deleted`` and erases/anonymizes that data.

- Owned events are hard-deleted (cascading to their occurrences and
  participant rows). A calendar event carries no third-party PII worth
  retaining, so deletion — not anonymization — is correct.
- The user's participations in *other* people's events are removed (their
  attendance is their PII), leaving those events intact for their owners.
- The user's availability windows are deleted.
"""
from stapel_core.gdpr import GDPRProvider


class CalendarGDPRProvider(GDPRProvider):
    section = "calendar"

    def export(self, user_id) -> dict:
        from .models import AvailabilityWindow, Event, Participant

        owned = list(
            Event.objects.filter(owner_id=user_id).values(
                "id", "title", "start", "end", "scope_key", "status", "rrule"
            )
        )
        participations = list(
            Participant.objects.filter(user_id=user_id).values(
                "event_id", "rsvp"
            )
        )
        windows = list(
            AvailabilityWindow.objects.filter(user_id=user_id).values(
                "weekday", "start_time", "end_time", "timezone"
            )
        )
        return {
            "owned_events": _serialize(owned),
            "participations": _serialize(participations),
            "availability_windows": _serialize(windows),
        }

    def delete(self, user_id) -> None:
        from .models import AvailabilityWindow, Event, Participant

        # Owned events cascade to their occurrences + participant rows.
        Event.objects.filter(owner_id=user_id).delete()
        # Attendance in other users' events is this user's PII — remove it.
        Participant.objects.filter(user_id=user_id).delete()
        AvailabilityWindow.objects.filter(user_id=user_id).delete()

    def anonymize(self, user_id) -> None:
        # Calendar rows carry no content that must be retained after deletion.
        pass


def _serialize(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, "isoformat") else str(v) for k, v in row.items()}
        for row in rows
    ]
