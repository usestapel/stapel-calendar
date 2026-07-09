"""Settings namespace for stapel-calendar.

All configuration is read through ``calendar_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_CALENDAR`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior.

The four documented extension seams (see MODULE.md):

- ``SCOPE_PROVIDER`` — resolves/filters the opaque ``scope_key`` from the
  request (a host may supply e.g. ``workspace_id``). The library itself is
  scope-agnostic.
- ``REMINDER_POLICY`` — decides *when* and *what* to remind; the default
  emits ``calendar.event.reminder_due`` for stapel-notifications to deliver.
- ``PRESETS`` — recurrence presets merged OVER the built-ins
  (none/daily/weekdays/weekly/biweekly/monthly/custom), so a host can add
  custom RRULE presets without restating the built-ins (``None`` removes).
- the *resource hook* is the ``calendar.occurrence.materialized`` comm
  emit (schemas/emits/) — no setting; app-layers subscribe.

``VISIBILITY`` is the one CTO-facing **config axis** (capability-config.md
§16): ``participants`` (default) scopes an event's read surface to its
invitees; ``scope`` opens every event to the whole scope the SCOPE_PROVIDER
resolves (the workspace/org/tenant). It is the ONE key surfaced as an axis in
capabilities.json — the rest are tuning knobs or extension seams.
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
        # Cap for virtual expansion of an unbounded series (no UNTIL/COUNT
        # in the RRULE and no explicit range end): how many days ahead of
        # the range start to expand for display.
        "DEFAULT_EXPANSION_HORIZON_DAYS": 90,
        # Hard safety cap on the number of occurrences a single expansion
        # may yield — protects against a pathological RRULE.
        "MAX_EXPANSION_OCCURRENCES": 1000,
        # Reminder offsets in minutes before an event's start that the
        # default policy fires a `calendar.event.reminder_due` for.
        "REMINDER_OFFSETS": [10],
        # Granularity (seconds) of the reminder cron scan — a reminder is
        # "due" when now falls in [fire_time, fire_time + this window).
        "REMINDER_SCAN_WINDOW_SECONDS": 60,
        # Dotted path to a ReminderPolicy — the reminder seam. The default
        # emits the reminder_due event; swap for a custom cadence/channel
        # decision without forking.
        "REMINDER_POLICY": "stapel_calendar.reminders.DefaultReminderPolicy",
        # Dotted path to a ScopeProvider — resolves the opaque scope_key
        # from a request and filters querysets by it. The default is a
        # no-op (single global scope); a host may return e.g. workspace_id.
        "SCOPE_PROVIDER": "stapel_calendar.scope.DefaultScopeProvider",
        # Recurrence presets merged OVER recurrence.BUILTIN_PRESETS. A value
        # is a dict of dateutil.rrule kwargs (freq/interval/byweekday/...);
        # None removes a built-in.
        "PRESETS": {},
        # Default slot length (minutes) for availability slot computation.
        "DEFAULT_SLOT_MINUTES": 30,
        # Visibility axis (capability-config.md §16). "participants": an event
        # is visible only to its invitees (the request user must be a
        # participant) — the historical, fail-closed default. "scope": events
        # are visible to the whole scope the SCOPE_PROVIDER resolves for the
        # request (workspace/org/tenant-wide calendars). Any value other than
        # "scope" is treated as "participants" — an unknown value degrades to
        # the restrictive default rather than accidentally exposing events.
        "VISIBILITY": "participants",
}

calendar_settings = AppSettings(
    "STAPEL_CALENDAR",
    defaults=DEFAULTS,
    import_strings=("REMINDER_POLICY", "SCOPE_PROVIDER"),
)

__all__ = ["calendar_settings"]
