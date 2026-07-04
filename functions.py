"""comm surface of stapel-calendar.

Every Function/Action carries a JSON schema in ``schemas/`` — tests run with
``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails loudly.
Registration happens on import from ``apps.py:ready()``.

Emits (see schemas/emits/):
- ``calendar.occurrence.materialized`` — a recurring occurrence gained its
  own state and was persisted; app-layers subscribe to attach a resource
  (legacy creates a Room). The engine creates no resource itself.
- ``calendar.event.reminder_due`` — a reminder for an event is due;
  stapel-notifications delivers it.

Functions (see schemas/functions/):
- ``calendar.free_busy`` — busy intervals for a user in a range (a
  scheduling primitive other services can call synchronously).
"""
from stapel_core.comm import function


@function("calendar.free_busy")
def free_busy(payload):
    """Return busy intervals for a user over a range.

    Input: ``{"user_id": str, "start": iso8601, "end": iso8601,
              "scope_key": str?}``.
    Output: ``{"busy": [{"start": iso8601, "end": iso8601}, ...]}``.
    """
    from django.contrib.auth import get_user_model
    from django.utils.dateparse import parse_datetime

    from . import services

    User = get_user_model()
    user = User.objects.get(pk=payload["user_id"])
    start = parse_datetime(payload["start"])
    end = parse_datetime(payload["end"])
    scope_key = payload.get("scope_key")
    busy = services.free_busy(user, start, end, scope_key=scope_key)
    return {
        "busy": [
            {"start": i.start.isoformat(), "end": i.end.isoformat()} for i in busy
        ]
    }
