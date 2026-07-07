# Changelog

All notable changes to stapel-calendar are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.2.2] - 2026-07-08

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_calendar.tests`
  subpackage is no longer listed in `[tool.setuptools] packages`). Added
  `[project.urls]`, completed the trove classifiers (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section (single source shared with the git hooks/CI).


## [0.2.0] — Unreleased

Timezone/DST-correctness and defensive-input hardening from the adversarial
review (`review-calendar-rrule`): exact interval math across DST
transitions, PEP 495-safe occurrence dedup, an occurrence **cancellation
model** (the RFC 5545 EXDATE analog the engine was missing), and validation
of hostile/degenerate input. Pre-1.0 minor: contains breaking changes —
see the migration notes below.

### Added
- **Occurrence cancellation/reschedule model (EXDATE / RECURRENCE-ID
  analog).** New `Event.recurrence_id` field: a materialized occurrence
  permanently claims its *original* rule instant, even after being
  rescheduled (`start`/`end` move, `recurrence_id` does not). A row with
  `status=CANCELLED` at a `recurrence_id` is a **tombstone**: expansion and
  free/busy skip both the concrete row and the virtual occurrence at that
  instant, so a cancelled slot can never silently resurrect.
  - New service `cancel_occurrence(series, occurrence_start)` — cancels one
    instant of a series (materializes a tombstone if needed, idempotent).
    A freshly created tombstone does **not** emit
    `calendar.occurrence.materialized` (a cancelled instant must not
    trigger app-layer resource creation).
  - A **rescheduled** occurrence no longer double-books: busy counts only
    the row's actual time; the virtual occurrence at the original instant
    stays suppressed. Expansion reports the moved row's real `start`/`end`
    with its `materialized_id`.
- **`materialize()` validates the instant.** `occurrence_start` must be an
  actual instant of the series rule (inside its UNTIL/COUNT bounds) —
  otherwise `InvalidRecurrence`. Pass `off_rule=True` to deliberately
  create an exception occurrence. Previously any datetime created a
  "ghost" row: invisible to expansion, still counted busy.
- **`truncated` flag for capped expansions.** When an expansion hits
  `MAX_EXPANSION_OCCURRENCES` inside the requested range, the cap is no
  longer silent (everything past it merely *looked* free — double-booking
  bait): new `expand_rule_detailed` / `expand_event_detailed` /
  `free_busy_detailed` / `compute_slots_detailed` return
  occurrences/intervals plus `truncated`; the availability API response
  and the `calendar.free_busy` Function output gained a `truncated` field.
  The existing list-returning functions are unchanged wrappers.
- New error key `error.400.calendar_invalid_slot_minutes`.
- Public recurrence helpers `as_utc()`, `add_duration()`,
  `is_rule_instant()`.

### Fixed (adversarial review — DST/defensive pass)
- **DST gap/fold occurrence intervals (H1).** `end = start + duration` was
  wall-clock arithmetic: a daily 02:30–03:00 America/New_York series
  produced an *inverted* interval on 2026-03-08 (spring-forward gap:
  start 07:30Z, end 07:00Z — and `materialize()` persisted the inverted
  row), and a 01:30–02:00 series produced a 1.5-hour interval on
  2026-11-01 (fall-back fold). Ends are now computed in instant (UTC)
  space (`add_duration`), preserving the exact series duration; inverted
  intervals are additionally discarded defensively by `merge_intervals`.
- **PEP 495 dedup miss on DST wall times (H2).** Virtual-vs-materialized
  dedup keyed a dict by raw `occ.start`; per PEP 495, inter-zone
  `==`/`hash` of gap/ambiguous wall times never match, so a materialized
  DST-transition occurrence was double-counted in free/busy and lost its
  `materialized_id` in the calendar view. Instants are now compared in
  UTC space on both sides.
- **Cancelled events counted busy (H3).** `free_busy` did not filter
  `status` at all; `DELETE` of a materialized occurrence resurrected the
  virtual one. Now: `status=CANCELLED` events/series/occurrences
  contribute nothing, and `DELETE /api/events/{id}` on a materialized
  occurrence tombstones it (response `{"status": "cancelled"}`) instead
  of deleting the row.
- **`slot_minutes` DoS (H4).** `GET /api/availability?slot_minutes=0` (or
  negative) hung the worker in an infinite slot loop; non-numeric values
  500ed. The view returns 400 (`calendar_invalid_slot_minutes`) and
  `compute_slots` raises `ValueError` for `slot_minutes < 1`.
- **Naive UNTIL stamped as UTC (M1).** A naive `until` was rendered with a
  `Z` suffix regardless of the event timezone, shifting the series
  boundary by the UTC offset (daily 04:00 MSK until 2026-01-10 03:00 MSK
  yielded Jan 8–10 instead of Jan 8–9). A naive `until` is now
  interpreted in the series' timezone (`dtstart` context) and converted;
  naive `dtstart` + naive `until` stays floating.
- **Poisoned series → calendar-wide 500s (M2).** Naive event start + aware
  `until` used to save fine and then throw dateutil's "UNTIL must be
  UTC" on *every* expansion. `build_rrule` now rejects the combination at
  create time (400), as well as a naive `until` without any `dtstart`
  context.
- **Zero-duration events shifted the slot grid (M4).** A zero-length busy
  point re-anchored the slot grid ([09:30, 10:30] instead of
  [09:00, 10:00, 11:00]) — degenerate intervals are dropped by
  `merge_intervals`. Contract: `end == start` (marker) events are allowed
  and occupy no time; `end < start` is rejected (`create_event` raises
  `ValueError`, the API returns 400 as before).
- **Availability window lost on range edges (M5).** `compute_slots`
  iterated days in the *range's* timezone; a Pacific/Auckland Monday
  08:00–10:00 window (= Sunday 20:00–22:00Z) on the last range day
  produced no slots. Days now iterate per-window in the window's own
  timezone.
- **COUNT+UNTIL together (L).** `build_rrule` now raises
  `InvalidRecurrence` (RFC 5545 §3.3.10 forbids both in one rule;
  dateutil silently applied UNTIL).
- **Busy intervals clipped to the requested range (L).** free/busy no
  longer returns interval parts outside `[start, end]`.

### Migration notes (0.1.0 → 0.2.0)
- **Run migrations.** `0002_recurrence_id` adds `Event.recurrence_id`,
  backfills it with `start` for existing occurrence rows, and re-keys the
  occurrence uniqueness constraint from `(recurrence_parent, start)` to
  `(recurrence_parent, recurrence_id)`.
- **Cancellation is soft.** `DELETE /api/events/{id}` on a materialized
  occurrence now returns `{"status": "cancelled"}` and keeps the row
  (status `cancelled`) instead of deleting it; the row remains visible in
  `events[]` / event detail with its status. Hosts that hard-delete
  occurrence rows via the ORM re-expose the virtual occurrence — use
  `cancel_occurrence()` / the API instead.
- **`materialize()` rejects off-rule instants.** Callers materializing
  computed occurrence starts are unaffected; callers passing arbitrary
  datetimes must pass `off_rule=True` or fix the instant.
- **`build_rrule` signature/semantics.** New `dtstart` kwarg (pass the
  series start — `create_event` does this for you). Naive `until` without
  `dtstart`, aware `until` with naive `dtstart`, and `count`+`until`
  together now raise `InvalidRecurrence`.
- **Cancelled events no longer count as busy** anywhere in
  availability/free-busy math; zero-duration events no longer block or
  split anything.
- **Additive**: `truncated` in the availability response and
  `calendar.free_busy` output; `error.400.calendar_invalid_slot_minutes`.

## [0.1.0] — 2026-07-05

Initial release. Generic calendar/recurrence/scheduling core extracted from
a prior backend during its Stapel migration (Ф3), and generalized so both
flavors of the domain — *meetings* and *bookings* (roadmap) — share one
core.

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

### Fixed (adversarial review)
- **Concurrent-materialize idempotency** — `materialize()` now handles the
  race where two callers materialize the same occurrence at once (the normal
  concurrent-booking case): the loser catches the `IntegrityError` from the
  `(recurrence_parent, start)` unique constraint, re-queries and returns the
  winner's row without re-emitting `calendar.occurrence.materialized`. Was a
  possible unhandled 500 under concurrency.
- **Bounded expansion** — `expand_rule` iterates lazily via `rrule.xafter`
  and stops at `max_occurrences`/range-end, so an unbounded rule over a huge
  range no longer computes the full in-range set before the cap trims it.
- **GDPR** — added a `user.deleted` consumer (`actions.py` +
  `CalendarGDPRProvider`) that erases the user's owned events (cascading to
  occurrences/participants), participations and availability windows;
  consumes schema in `schemas/consumes/`.
- Removed dead `_WEEKDAY_OBJECTS` in `recurrence.py`.

### Fixed (recurrence-correctness — vs the prior source)
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

[0.2.0]: https://github.com/usestapel/stapel-calendar/releases/tag/v0.2.0
[0.1.0]: https://github.com/usestapel/stapel-calendar/releases/tag/v0.1.0
