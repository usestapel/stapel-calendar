"""The Art. 15 export, and the registry seam onto the Art. 17 erasure.

The erasure itself is not here: it lives in :mod:`stapel_calendar.erasure`,
as one function the in-process registry (below) and the comm subscribers
registered in ``apps.ready()`` both reach. Two callers, one implementation —
a monolith and a fleet erase the same rows the same way, and there is no
second erasure to drift.

What is erased: owned events are hard-deleted (cascading to their
occurrences and participant rows), the subject's participations in *other*
people's events are removed (their attendance is their PII, the event is not
theirs), and their availability windows go.
"""
from stapel_core.gdpr import GDPRProvider

from .erasure import erase_subject


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
        """Erase the subject — the same operation the comm path runs.

        The registry reaches the erasure here; the
        ``gdpr.erasure.requested`` subscriber registered in ``apps.ready()``
        reaches it there. A host that runs both in one process erases once:
        whichever path arrives second finds nothing left and receipts its
        zeroes, and this one receipts nothing at all — the orchestrator
        records the local pass itself.
        """
        erase_subject("account", user_id)

    def anonymize(self, user_id) -> None:
        # Calendar rows carry no content that must be retained after deletion.
        pass


def _serialize(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, "isoformat") else str(v) for k, v in row.items()}
        for row in rows
    ]
