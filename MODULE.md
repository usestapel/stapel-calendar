# stapel-calendar — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- **Event / Participant / RSVP** — the generic calendar core. `Event`
  carries `title/description/start/end` (tz-aware), an `owner`, an opaque
  `scope_key` (workspace/org/tenant — the library is scope-agnostic, there
  is **no FK to Organization or Room**), and a `status`. `Participant`
  holds an RSVP (`invited/accepted/tentative/declined`); reminders are
  event-driven, **not** a `notified` boolean.
- **Recurrence engine (RRULE)** — recurrence is a canonical RFC 5545 RRULE
  string on the series master. Presets (`none/daily/weekdays/weekly/
  biweekly/monthly/custom`) build the RRULE via `python-dateutil`. Supports
  **virtual expansion** (compute a range for display, nothing persisted)
  and **on-demand materialization** (persist an occurrence only when it
  gains its own state — an RSVP or a host resource), with a series↔occurrence
  link. Interval math is instant-based (DST gap/fold safe); occurrence
  instants are compared in UTC (PEP 495 safe).
- **Cancellation/reschedule model (EXDATE / RECURRENCE-ID analog)** — a
  materialized occurrence claims its original rule instant via
  `Event.recurrence_id`; `status=CANCELLED` at an instant is a *tombstone*
  (skipped by expansion and free/busy, cannot resurrect). Cancel one
  instant with `services.cancel_occurrence()` or `DELETE /api/events/{id}`
  on the materialized occurrence (which tombstones, not deletes).
  Rescheduling = move the row's `start`/`end`; `recurrence_id` keeps
  suppressing the original instant so busy never doubles.
- **Availability** — recurring working windows + free/busy query + slot
  computation (the booking primitive). Expansions that hit
  `MAX_EXPANSION_OCCURRENCES` inside the range report `truncated` (the
  `*_detailed` service variants, the availability response and the
  `calendar.free_busy` output) — never treat the tail of a truncated
  range as free.
- **ICS export** — RFC 5545 VCALENDAR/VEVENT, with a minimal parser for a
  round-trip.
- **API** — events CRUD, `respond` (RSVP), user-calendar (events + expanded
  occurrences in a range), availability, ICS. DTO/DAO + serializer seams +
  OpenAPI (drf-spectacular).
- **comm surface** — emits `calendar.occurrence.materialized` (resource
  hook) and `calendar.event.reminder_due` (reminder delivery); provides the
  `calendar.free_busy` Function.

**Meetings vs bookings.** These are two flavors of one domain. A host app
supplies the *meetings* flavor (an occurrence's resource is a video Room);
the roadmap's *bookings* flavor uses the same core (availability windows +
slots + RSVP). Everything flavor-specific is pushed to the seams below.

## Extension points (fork-free)

### 1. Resource hook — `calendar.occurrence.materialized` (comm emit)

When a recurring occurrence is materialized, the engine emits
`calendar.occurrence.materialized` (and sends the `occurrence_materialized`
Django signal for in-process hosts). **The engine creates no app resource
itself** — the app-layer subscribes and creates a `Room`, pinning it
to the occurrence via the emitted `event_id`. This is the exact coupling the
extraction removed (the source created `rooms.models.Room` inside the
recurrence loop). Schema: `schemas/emits/calendar.occurrence.materialized.json`.

Two hook caveats:

- **Tombstones don't fire the hook.** `cancel_occurrence()` on a
  not-yet-materialized instant persists the cancelled row *without*
  emitting — a cancelled instant must not trigger resource creation.
- **With `OUTBOX_ENABLED=False`** (synchronous in-process delivery) the
  emit and the Django signal fire *inside* the materialize transaction: if
  a signal subscriber then raises, the occurrence row rolls back but the
  emit was already delivered — a phantom `calendar.occurrence.materialized`
  for a row that does not exist. With the outbox enabled, delivery is
  transactional and this cannot happen. Keep signal subscribers
  non-throwing, or run with the outbox in production.

### 2. Reminder policy — `REMINDER_POLICY` (dotted path, replace)

A `ReminderPolicy` decides *when* and *what* to remind. The default
(`DefaultReminderPolicy`) fires one `calendar.event.reminder_due` per
configured offset once the fire time enters the cron scan window; a host
calls `reminders.run_reminders(now)` on a schedule. Every emit carries a
stable `dedup_key` (`"<event_id>:<offset>"`) — dedup and delivery are the
notifications module's job. Subclass to change cadence/channel.

### 3. scope_key provider — `SCOPE_PROVIDER` (dotted path, replace)

A `ScopeProvider` (`resolve(request) -> scope_key`, `filter(qs, request)`)
resolves the opaque scope from the request and filters querysets. Default is
a no-op single global scope; a host may return the active `workspace_id`.

### 4. Recurrence presets — `STAPEL_CALENDAR["PRESETS"]` + `register_preset()` (open registry, MERGE)

Custom recurrence rules beyond the built-ins. Presets are **merged over**
`recurrence.BUILTIN_PRESETS`; a value is a dict of `dateutil.rrule` kwargs;
setting a name to `None` removes a built-in.

### Settings — `STAPEL_CALENDAR` namespace (`conf.py`)

Resolution order per key: `settings.STAPEL_CALENDAR[key]` -> flat Django
setting -> environment variable -> default. Read lazily at call time.

| Key | Default | What it customizes | Semantics |
|---|---|---|---|
| `SCOPE_PROVIDER` | `stapel_calendar.scope.DefaultScopeProvider` | Scope resolution/filtering | replace (dotted path) |
| `REMINDER_POLICY` | `stapel_calendar.reminders.DefaultReminderPolicy` | Reminder cadence/emit | replace (dotted path) |
| `PRESETS` | `{}` | Recurrence presets | **merge** over built-ins (`None` removes) |
| `REMINDER_OFFSETS` | `[10]` | Minutes-before-start the default policy fires; also bounds the cron scan lookahead | value |
| `REMINDER_SCAN_WINDOW_SECONDS` | `60` | Cron scan granularity | value |
| `DEFAULT_EXPANSION_HORIZON_DAYS` | `90` | Default range end when none given | value |
| `MAX_EXPANSION_OCCURRENCES` | `1000` | Safety cap on one expansion | value |
| `DEFAULT_SLOT_MINUTES` | `30` | Default slot length | value |
| `VISIBILITY` | `participants` | Event read surface: `participants` (invitees only) or `scope` (whole resolved scope) | **axis** (participants\|scope; capability-config.md §16) |

`VISIBILITY` is the module's one CTO-facing config axis (surfaced in
`docs/capabilities.json`): `participants` (default, fail-closed) limits an
event to its invitees; `scope` opens events to the whole scope the
`SCOPE_PROVIDER` resolves. An unknown value degrades to `participants`.

### Serializer seams (`views.py`)

`SerializerSeamMixin` — subclass a view, set `request_serializer_class` /
`response_serializer_class`, remount the URL.

| View | Request serializer | Response serializer |
|---|---|---|
| `EventListCreateView` | `EventCreateRequestSerializer` | `EventResponseSerializer` |
| `EventDetailView` | — | `EventResponseSerializer` |
| `EventRespondView` | `RSVPRequestSerializer` | `EventResponseSerializer` |
| `CalendarView` | — | `CalendarResponseSerializer` |
| `AvailabilityView` | — | `AvailabilityResponseSerializer` |
| `EventICSView` | — | (raw `text/calendar`) |

### Events & functions (comm surface)

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Emit | `calendar.occurrence.materialized` | `{event_id, series_id, scope_key, owner_id, title, start, end}` | `schemas/emits/calendar.occurrence.materialized.json` |
| Emit | `calendar.event.reminder_due` | `{event_id, scope_key, owner_id, title, start, offset_minutes, participant_ids, dedup_key}` | `schemas/emits/calendar.event.reminder_due.json` |
| Function (provides) | `calendar.free_busy` | `{user_id, start, end, scope_key?}` -> `{busy: [{start, end}], truncated}` | `schemas/functions/calendar.free_busy.json` |

### API contract notes

- **`CalendarView` returns a materialized occurrence twice by design**: as
  a concrete row in `events[]` (it *is* an Event) and as an entry in
  `occurrences[]` (`is_materialized=true`). Clients must dedup by
  `occurrences[].materialized_id == events[].id`. Cancelled (tombstoned)
  occurrences appear only in `events[]` with `status=cancelled`, never in
  `occurrences[]`.
- **Zero-duration events** (`end == start`) are valid markers: they are
  stored and listed but occupy no time — no busy interval, no slot-grid
  effect. `end < start` is rejected with 400.
- **Availability**: `slot_minutes` must be a positive integer (400
  `error.400.calendar_invalid_slot_minutes` otherwise); the response's
  `truncated=true` means a series expansion hit
  `MAX_EXPANSION_OCCURRENCES` inside the range and later times only look
  free.

### Admin categories — `@access` declarations (admin-suite AS-5)

Every model in `models.py` carries (or implicitly defaults to) a
`stapel_core.access.access` category — one declaration, consumed by admin
visibility, default staff rights, and the audit report (admin-suite §0).
Undecorated = `business` (visible, staff-manageable) and is the correct,
zero-effort default for domain tables.

All three models here (`Event`, `Participant`, `AvailabilityWindow`) are
`business` and stay undecorated — none fit `ops` (outbox/dedup/audit-log/
TTL-junk machinery, nobody-edits-this-through-the-admin journals) or `secret`
(token/key/credential carriers). No ICS-feed access token, calendar-sync
webhook secret, or external-sync dedup/audit-log model exists in this
library to warrant otherwise.

### Contract emission — the `schema` + `flows` + `errors` triad

This module emits its **own** machine-readable API contract, per-module, so
the frontend codegen (and any host migrating to `stapel-calendar`) reads a
committed, version-pinned artifact instead of checking out a floating-`main`
aggregate (contract-pipeline.md §2, verdict **A**). Copied from stapel-auth's
reference implementation (contract-pipeline.md §2-3, ETALON) and
stapel-profiles' adaptation. The triad lives in `docs/`:

```
docs/schema.json   drf-spectacular OpenAPI, this module only, canonical /calendar/api/ prefix
docs/flows.json    generate_flow_docs machine artifact — [] (no @flow_step here)
docs/errors.json   generate_error_keys registry (unchanged by this addition)
```

**stapel-calendar is not yet mounted in `stapel-example-monolith`**
(grep-confirmed — no monolith `urls.py` references `stapel_calendar`), so
unlike auth/profiles there is no aggregate slice to diff `docs/schema.json`
against for byte-identity. Standalone validation substitutes
(contract-pipeline.md §9 fallback):

- **determinism** — two independent `make contract` runs are byte-identical;
- **self-contained `$ref` closure** — every component reference reachable
  from a path resolves inside this one document (zero dangling refs); no
  sibling module needed to be co-mounted for closure;
- **security** — every operation (all six calendar views require
  `IsAuthenticated`) carries `security: [{"JWTCookieAuth": []}]`;
- **canonical prefix** — schema paths and flow endpoints are mounted at
  `/calendar/api/*`, matching the `<mod>/api/` shape every pair-backend uses,
  derived from this module's own `urls.py` docstring
  (`path("calendar/", include("stapel_calendar.urls"))`).

`tests/test_contract.py` asserts all four; a dormant
`test_matches_monolith_calendar_slice` (unconditionally skipped) is wired for
the day calendar *is* mounted in the monolith, mirroring auth/profiles — do
not fabricate a slice to make it pass early.

**Harness** (`_codegen_settings.py` / `codegen_urls.py` / `_codegen.py`,
`make contract` / `make contract-check`): same shape as stapel-auth's, with
one addition specific to calendar (shared with profiles) —
`_codegen.py` explicitly calls
`stapel_core.django.openapi.swagger._register_jwt_auth_extension()` before
emitting. A real host registers this drf-spectacular extension (the
`JWTCookieAuth` security scheme) as a side effect of its own dev-only Swagger
URLs; that registration is *global* process state, not tied to any one
module's urls.py. stapel-auth's harness gets it for free only because its
co-mounted sibling (`stapel_gdpr.urls`) happens to call
`get_app_swagger_urls()` unconditionally — calendar has no such sibling (it
mounts alone), so without the explicit call, protected endpoints would emit
without their `security: [{"JWTCookieAuth": []}]` entry.

Regenerate after any serializer/view/url/error change:

    make contract        # or: python -m stapel_calendar._codegen --out docs

then commit `docs/{schema,flows,errors}.json`.

## Anti-patterns

- **Don't create app resources inside the recurrence engine.** Subscribe to
  `calendar.occurrence.materialized` instead — that boundary is the whole
  point of this module.
- **Don't add a `notified` boolean.** Reminders are event-driven; dedup
  belongs to the delivering consumer via `dedup_key`.
- **Don't compute monthly recurrence with `timedelta(days=30)`.** Use a
  preset -> RRULE; `FREQ=MONTHLY` is calendar-correct (month-end handled).
- **Don't hard-delete a materialized occurrence row** (ORM `.delete()`) —
  the virtual occurrence at its instant resurrects and the slot becomes
  busy again. Cancel it: `cancel_occurrence()` or the DELETE endpoint
  (both tombstone via `status=CANCELLED` + `recurrence_id`).
- **Don't do wall-clock datetime arithmetic on occurrence instants.** Use
  `recurrence.add_duration()` (instant space; DST gap/fold safe) and
  compare instants via `recurrence.as_utc()` (PEP 495: raw `==`/dict keys
  miss on DST-transition wall times).
- **Don't put workspace/org FKs on `Event`.** The scope is the opaque
  `scope_key`; resolution is the `SCOPE_PROVIDER` seam.
- **Don't import other stapel modules** — cross-module is comm by string name.
- **Don't bypass the settings namespace** with `os.getenv` at import time.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam above: a
settings key, a subclass + URL remount, a comm subscriber, a custom preset.

**Upstream contribution** if it needs new model fields/migrations, new
endpoints, a new settings key or seam, or changes a committed schema.

Litmus test: if you'd have to monkeypatch or edit code inside
`stapel_calendar/` — it's upstream. If a setting, subclass, receiver or comm
call gets you there — it's app-layer.
