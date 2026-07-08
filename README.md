# stapel-calendar

[![CI](https://github.com/usestapel/stapel-calendar/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-calendar/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-calendar/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-calendar)

Calendar, recurrence and scheduling for the [Stapel framework](https://github.com/usestapel) —
composable Django apps that deploy as a monolith or as microservices
without changing module code.

A generic calendar core — **Event / Participant / RSVP**, an RFC 5545
**recurrence engine** (RRULE via `python-dateutil`, virtual expansion +
on-demand materialization), **availability** (working windows, free/busy,
slots) and **ICS export**. Two flavors of one domain — *meetings* and
*bookings* — share this core; everything app-specific lives behind seams.

## Install

```bash
pip install stapel-calendar
```

```python
INSTALLED_APPS = [
    # ...
    "stapel_calendar",
]

# urls.py
path("calendar/", include("stapel_calendar.urls"))
```

## Concepts

- **Event** — a scheduled thing (meeting/booking/appointment) with tz-aware
  `start`/`end`, an `owner` and an opaque `scope_key`. No FK to any host
  concept (org, room) — scoping and resources are seams.
- **Recurrence** — the series master stores a canonical RRULE. Occurrences
  are *virtual* until they gain their own state (an RSVP or a resource), at
  which point they are *materialized* (persisted) on demand.
- **Availability** — recurring working windows minus busy events give open
  booking slots.

```python
from stapel_calendar import services

series = services.create_event(
    owner=user, title="Standup",
    start=start, end=end,
    recurrence_type="weekdays",            # -> FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR
)
occurrences = services.expand_event(series, range_start, range_end)  # virtual
services.materialize(series, occurrence_start)  # persist one + emit hook
busy = services.free_busy(user, range_start, range_end)
slots = services.compute_slots(user, range_start, range_end, slot_minutes=30)
```

## Settings

All configuration lives in the `STAPEL_CALENDAR` namespace (dict setting,
flat setting, or env var — resolved lazily):

| Key | Default | Meaning |
|---|---|---|
| `SCOPE_PROVIDER` | `…scope.DefaultScopeProvider` | Resolve/filter the opaque `scope_key` |
| `REMINDER_POLICY` | `…reminders.DefaultReminderPolicy` | When/what to remind |
| `PRESETS` | `{}` | Custom recurrence presets (merged over built-ins) |
| `REMINDER_OFFSETS` | `[10]` | Minutes before start to remind |
| `REMINDER_SCAN_WINDOW_SECONDS` | `60` | Reminder cron granularity |
| `DEFAULT_EXPANSION_HORIZON_DAYS` | `90` | Default range end |
| `MAX_EXPANSION_OCCURRENCES` | `1000` | Expansion safety cap |
| `DEFAULT_SLOT_MINUTES` | `30` | Default slot length |

## comm surface

| Kind | Name | Contract |
|---|---|---|
| Emit | `calendar.occurrence.materialized` | An occurrence was persisted — subscribe to attach a resource |
| Emit | `calendar.event.reminder_due` | A reminder is due — deliver it |
| Function | `calendar.free_busy` | `{user_id, start, end, scope_key?}` -> `{busy: [...]}` |

## Extension points

See [MODULE.md](MODULE.md) — the agent-facing map of every fork-free seam
(the resource hook, reminder policy, scope provider, recurrence presets,
serializer seams, settings).

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff
./setup-hooks.sh
pytest tests/
```

## License

MIT
