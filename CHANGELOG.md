# Changelog

All notable changes to stapel-calendar are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.1.0] — Unreleased

Initial release. Generic calendar/recurrence/scheduling core extracted from
the legacy backend (`calendar_app`) during its Stapel migration (Ф3), and
generalized so both flavors of the domain — *meetings* (legacy) and
*bookings* (roadmap) — share one core.

### Added
- **Event / Participant / RSVP** models. `Event` is scope-agnostic (opaque
  `scope_key`, no FK to Organization/Room) with a UUID id, owner, tz-aware
  `start`/`end` and status. RSVP is `invited/accepted/tentative/declined`.
- **RFC 5545 recurrence engine** (`recurrence.py`) — canonical RRULE strings
  built from presets (`none/daily/weekdays/weekly/biweekly/monthly/custom`)
  via `python-dateutil`; **virtual expansion** and **on-demand
  materialization** with a series↔occurrence link; an open preset registry
  (settings `PRESETS` + `register_preset()`).
- **Availability** (`services.py`) — recurring working windows, free/busy
  query, and slot computation.
- **ICS export** (`ics.py`) — RFC 5545 VCALENDAR/VEVENT + minimal parser.
- **API** — events CRUD, `respond` (RSVP), user-calendar, availability, ICS;
  DTO/DAO, serializer seams, OpenAPI.
- **comm surface** — emits `calendar.occurrence.materialized` (resource
  hook) and `calendar.event.reminder_due`; provides `calendar.free_busy`.
- **Four extension seams** — resource hook (occurrence.materialized),
  reminder policy (`REMINDER_POLICY`), scope provider (`SCOPE_PROVIDER`),
  recurrence presets (`PRESETS`). System checks on all seam config.

### Fixed (recurrence-correctness — vs the legacy source)
- **Monthly recurrence** now uses `FREQ=MONTHLY` (calendar-correct: a series
  on the 31st yields Mar/May/… and skips short months) instead of the
  source's `timedelta(days=30)`, which drifted off the day-of-month.
- **Materialization** is virtual + on-demand, replacing the source's eager
  persistence of every occurrence.
- **Custom weekdays** normalize to RRULE `BYDAY`, replacing the source's raw
  `recurrence_days` CSV string.
- **Participant copy** on materialization is a single batch `bulk_create`,
  not a per-occurrence loop of individual inserts.
- **Reminders** are event-driven (`calendar.event.reminder_due` with a
  `dedup_key`), replacing the source's `notified` boolean and absent scheduler.
- **Resource decoupling** — the recurrence engine no longer creates app
  resources (the source created `rooms.models.Room` inside the loop); it
  emits `calendar.occurrence.materialized` for an app-layer subscriber.
- **DST / month-end** correctness is pinned by tests (Jan 31 → Mar 31,
  US spring-forward keeps wall-clock time).

[0.1.0]: https://github.com/usestapel/stapel-calendar/releases/tag/v0.1.0
