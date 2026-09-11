# Changelog

All notable changes to stapel-calendar are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.7.1] — 2026-09-11

### Fixed — an event title could put a property into somebody else's calendar

Security audit 2026-09-11, L-7. `ics._ESCAPES` covered backslash, semicolon,
comma and LF, and not CR. A title is attacker-supplied text on every surface
that lets one person invite another, and line endings in RFC 5545 are CRLF,
so a title carrying `\rATTENDEE:mallory@example.net` reached a CR-splitting
reader as a **new property** on the event — an added attendee, or with
`\rORGANIZER:`/`\rURL:` something worse. `_escape` collapses `\r\n` and a lone
`\r` onto the one escaped newline the format has, so a Windows line break is
one break and not two, and the round-trip through `parse_ics` is unchanged.

### Fixed — content lines are folded at 75 octets

Same section of the same standard (§3.1): a content line is at most 75
octets, and a reader meeting a longer one may truncate it or give up on the
file. Nothing folded, and an export of a user-typed 300-character title
produces exactly that line. `ics._fold` folds with CRLF + one space — the
continuation `parse_ics` and every other reader already strips — splitting on
octets and never inside a UTF-8 character, because a folded half that is not
valid UTF-8 on its own is a file some readers refuse outright. Applied once,
in `to_ics`, so every producer above it still writes one logical line.

## [0.7.0] — 2026-08-30

### Fixed — a merge is not a delete: the guest's calendar no longer vanishes at sign-in

This module knew half of an account's life cycle. `user.deleted` was
answered from the first release; `user.merged` — stapel-auth folding an
anonymous guest into an existing account when the guest signs in — was not
answered at all, and silence there is not neutrality, it is a wrong answer
given quietly.

stapel-auth deletes the absorbed guest row, and `Event.owner`,
`Participant.user` and `AvailabilityWindow.user` are all `CASCADE`. So a
visitor who booked a slot, RSVP'd to an invitation or published availability
before signing in lost every bit of it the moment they signed in with an
account that already existed. Nothing raised, nothing retried, nothing was
logged, and no erasure was ever requested for the rows — the first report is
a person saying the booking they made is gone.

`stapel_calendar.actions.handle_user_merged` now carries the guest's
calendar onto the survivor in one transaction:

- **`Event.owner`** — plain rewrite. Materialized occurrences are `Event`
  rows carrying their own owner, so masters and occurrences move together;
  `cal_event_uniq_occurrence` is scoped to `(recurrence_parent,
  recurrence_id)` and cannot be tripped by a change of owner.
- **`Participant.user`** — deduplicated against `cal_participant_uniq`
  first. Both accounts may be invited to one event, and there the survivor's
  own row wins with the RSVP they answered as themselves; the guest's
  duplicate is dropped rather than reassigned into a constraint violation.
- **`AvailabilityWindow.user`** — plain rewrite. No user-scoped constraint,
  and overlapping windows are legal (free/busy unions them), so two
  accounts' windows simply add up.

Other people's events are untouched: only the guest's own participation rows
move, the same line `erase_subject` draws.

Two "unknown id" situations that must not be conflated. A guest who owns
nothing here is a quiet no-op — the common case, and also the at-least-once
idempotency path. A guest who owns rows while the survivor has no user row
here yet raises `MergeTargetNotReady`, which the comm layer turns into a
redelivery: returning success would let the outbox mark the event delivered
and lose the calendar for good. A malformed id is neither — it names no row
and no redelivery can fix it, so it is logged and dropped instead of
replayed as a poison pill (`UUIDField` raises `ValidationError`, which is
not a `ValueError`; both are caught).

### Changed — `stapel-core>=0.52.1`

Core 0.52.1 adds the `stapel_core.lifecycle.E001` system check (tag
`stapel_lifecycle`): an app that subscribes `user.deleted` and not
`user.merged` is a boot-time ERROR. This module's `user.deleted` subscriber
is a closure core registers on its behalf from `register_gdpr_owner`, and
core stamps it with this module's name so the pair is charged here rather
than to core — which is exactly what `tests/test_user_merged.py::
TestSubscription::test_the_lifecycle_pair_check_is_green` asserts. The floor
is raised so that gate can never be skipped for want of the check.

## [0.6.1] — 2026-08-24

### Fixed — the emitted contract had no range dimension at all

`GET /events`, `GET /calendar` and `GET /availability` read `start` / `end`
from `request.query_params`, and availability reads `slot_minutes` — none of
it declared. Being plain `APIView`s, nothing inferred them, so
`docs/schema.json` emitted `"parameters": null` for all three operations:
**the whole time window and slot granularity of this module were invisible to
the contract.** It worked only because the frontend pair hand-wrote the two
names. The moment a client is generated from the schema, an undeclared
parameter is a parameter that does not exist — and a calendar that can only
ever ask for the server's default window is not a calendar.

- All four are now declared with `@extend_schema(parameters=...)`, carrying
  their type, their default (now .. now + `DEFAULT_EXPANSION_HORIZON_DAYS`;
  `DEFAULT_SLOT_MINUTES` for slots) and the refusal they cause
  (`error.400.calendar_invalid_range`,
  `error.400.calendar_invalid_slot_minutes`).
- `tests/test_contract.py::test_range_query_parameters_are_declared` pins it,
  and requires every query parameter to carry a type **and** a description —
  so the hole cannot silently reopen on the next read that takes a window.

### Fixed — `AvailabilityResponse.truncated` shipped half its warning

The emitter takes one line per attribute from the DTO docstring, and
`truncated`'s warning was wrapped across three — so the contract said
`"True when a series expansion hit the"` and stopped. Half that sentence is
worse than none: a client reading it has a boolean it cannot render, on the
one field whose entire purpose is to say *this answer is incomplete, later
times only LOOK free*. The docstring line is now one line, the whole sentence
reaches the schema, and a test pins it.

No behaviour changed; the only diff outside docs is the parameter
declarations and the docstring.

## [0.6.0] — 2026-08-24

### Fixed — the erasure answers its probe: stapel-calendar registers as a GDPR data owner

Found on a live stand: this module was a **declared** data owner that
answered no `gdpr.owner.probe`. Its `CalendarGDPRProvider` was registered
in-process and its `user.deleted` handler really did erase, so a monolith
looked fine — and a fleet's owners-health said `calendar: alive=false`
forever, and every erasure request waited on this owner until it timed out.
Liveness is answered by the subscriber that erases, and there was none to
answer.

`apps.ready()` now hands one callable to `stapel_core.gdpr.register_gdpr_owner`
and core subscribes the whole protocol: `gdpr.erasure.requested` ->
`gdpr.section.erased` (deterministic `receipt_id`, receipt emitted inside the
erase's transaction), `gdpr.owner.probe` -> `gdpr.owner.alive` from the same
module, and the deprecated `user.deleted`. **No protocol code is written
here.**

- **Owner** `calendar` (= `CalendarGDPRProvider.section`), **subject types**
  `["account"]` — every row hangs off one user id, and `scope_key` is an
  opaque host-computed string, not a workspace id a `workspace` erasure could
  match on.
- **Counts** `{events, participations, availability_windows}`: owned events
  hard-deleted (cascading to occurrences and to the participant rows on
  them), the subject's participations in other people's events removed, their
  availability windows deleted. Idempotent — a redelivery receipts its zeroes
  and mints the same `receipt_id`.
- New `stapel_calendar/erasure.py`: `OWNER`, `SUBJECT_TYPES`,
  `erase_subject(subject_type, subject_key, workspace_id=None)`. The
  in-process provider and all three subscribers reach this one counted
  function, so a monolith and a fleet erase the same rows the same way.

### Removed — `stapel_calendar.actions.handle_user_deleted`

**Breaking** (pre-1.0: minor = breaking) for anyone importing that handler
directly. The signal is still consumed — by the subscriber
`register_gdpr_owner` installs, running the same `erase_subject("account",
…)` — and it now also emits the `gdpr.section.erased` receipt the old handler
never sent. Two handlers for one signal is two erasures to keep in step.

### Changed — `stapel-core` floor raised to 0.35.0

`stapel_core.gdpr.register_gdpr_owner` exists only in 0.35.0.

### Added — comm contracts for the protocol

`schemas/consumes/gdpr.erasure.requested.json`,
`schemas/consumes/gdpr.owner.probe.json`, `schemas/emits/gdpr.owner.alive.json`,
`schemas/emits/gdpr.section.erased.json`. `schemas/consumes/user.deleted.json`
loses its `format: uuid` on `user_id` — a host's user pk is an integer as
often as a UUID, and the erasure takes both spellings as they come.

No new settings; `CONFIG.MD` is unchanged.

## [0.5.0] — 2026-08-16

### Security — the by-id reads need a mandate, not merely a real account

`EventDetailView` and `EventICSView` resolve through the scope provider only,
with no participation filter, so any caller the gate admits can read any event
by UUID. 0.4.x closed the anonymous axis with `IsNotAnonymousUser` because the
vocabulary had two words — but that class admits every real account, including
one belonging to no workspace anywhere, and minting an account is cheap.

Both now carry `HasWorkspaceMandateIfScoped`: the gate that asks the third
question, in the variant a *library* view needs, so a genuinely single-tenant
host — where no mandate exists to hold — keeps working instead of answering
503 to everyone. Mutations were already owner-only, so nothing is lost by
closing the whole view.

**Breaking** (pre-1.0: minor = breaking): in a workspace-bearing deployment, an
account with no mandate anywhere can no longer fetch an event by UUID or export
its `.ics`. The declared permission changed on four published operations.

### Changed — `stapel-core` floor raised to 0.27.0

`HasWorkspaceMandateIfScoped` exists only in 0.27.0, which also owns
`error.503.mandate_unavailable` in the committed `docs/errors.json`.

## [0.4.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it, so an older core regenerated the artifact without `owner` and the
drift gate went red.

## [0.4.1] — 2026-08-02

### Added
- `docs/llms.txt` — the fifth contract artifact, an agent-sized slice of the
  schema/flows/errors/capabilities triad, wired into `make contract` /
  `make contract-check` (badge-canon §3).
- Badge canon in README, classifier 3.14.

### Fixed
- `docs/capabilities.json`, `docs/flows.json`, `docs/errors.json`,
  `docs/llms.txt` and `CONFIG.MD` now ship in the wheel via `package-data`
  (#184); previously repo-only, invisible to `--from-installed` tooling.

## [0.4.0] — 2026-07-30

### Every view says, in its own source, what a guest may do (#168)

`stapel-core` 0.16 turns the `AUTH_ANONYMOUS` axis into a question this
module never answered. A guest session is `is_authenticated`, so a bare
`IsAuthenticated` gate lets it through — and all seven views here were gated
on exactly that, saying nothing about whether that was wanted.
`stapel_core.adoption` W002 reported all seven against a real deployment.

The answer is drawn along the line this module's own filtering already draws:

> **a guest may read the calendar it is in — which is none — and may not open
> a single named event, nor write anything.**

**Open, deliberately** (`stapel_anonymous_access = ANONYMOUS_ALLOWED`):

- `GET /events`, `GET /calendar` — bounded by `_visible_events`, i.e. by the
  `VISIBILITY` axis and the deployment's scope provider. A guest gets an
  empty range, which is the truth. **This is a live guest path**: a real
  consumer (a meeting app) renders the guest's one and only page with
  `GET /calendar/api/v1/events` on it, so closing it would have turned a
  guest's landing page into an error in production.
- `GET /availability` — computed strictly over `request.user`'s own
  free/busy; a guest is reported entirely free, revealing nobody else's time.

**Closed** (`IsNotAnonymousUser` — 403 where an anonymous session previously
got through):

- `GET /events/{id}` and `GET /events/{id}/ics` resolve through the scope
  provider **only**, with no participation filter — so with the stock no-op
  provider any authenticated caller can fetch any event by UUID. The
  anonymous axis makes "authenticated" free to obtain, which turns that
  looseness into an open door. Their mutating halves (`PATCH`/`DELETE`) are
  owner-only and a guest owns nothing, so nothing is lost by closing the
  views whole.
- `PUT /events/{id}/participants` — an organizer action, and the organizer is
  the owner.
- `POST /events/{id}/respond` — an RSVP presupposes an invitation, and a
  guest has no address to be invited at (this was already a 404
  `not_invited`; now it is a refusal at the door).
- `POST /events` is guarded inside the method, because the view's `GET` half
  must stay reachable. An event is durable and owned; a series created under
  a throwaway account keeps materializing occurrences long after the only
  identity that could cancel it is gone.

Minor per this project's pre-1.0 rule (minor = breaking): for a deployment
with `AUTH_ANONYMOUS` on this changes live behaviour, and it is visible in
the published contract — `docs/schema.json` now documents
`IsNotAnonymousUser` on the four closed operations. Deployments without
guest sessions are unaffected: an ordinary authenticated user passes
`IsNotAnonymousUser` exactly as before.

New `tests/test_guest_surface.py` pins both halves — what a guest must still
reach, and what it must not.

### Changed

- Minimum `stapel-core` raised to `>=0.16` (the release that added
  `ANONYMOUS_ALLOWED` / `ANONYMOUS_DENIED`).

## [0.3.9] — 2026-07-30

- Widen the `stapel-core` cap to `<1.0`.

## [0.3.8] — 2026-07-17

Fix-up #2: 0.3.7's regen still baked the old version into
`docs/capabilities.json` (`make contract` ran before the version bump
landed). Re-ran with 0.3.8 already in `pyproject.toml`; verified match,
suite green.

## [0.3.7] — 2026-07-17

Fix-up: 0.3.6's CI/publish failed on contract drift — `docs/capabilities.json`
embeds the package version and wasn't regenerated for the 0.3.6 bump.
Regenerated via `make contract`; no other diff.

## [0.3.6] — 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.3.5] — 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules). Contract artifacts regenerated (version bump);
  suite green.

## [0.3.3] — 2026-07-16

### Fixed
- **`update_event` no longer silently unbounds a recurring series on a
  partial update.** The RRULE of a series master was rebuilt from *only* the
  explicitly sent fields, so moving `start` without re-sending
  `recurrence_until` / `recurrence_weekdays` / `recurrence_interval` /
  `recurrence_count` dropped them — a bounded series quietly became infinite.
  Unsent recurrence params are now recovered from the stored canonical RRULE
  (new `recurrence.rrule_inputs`) and merged under the sent ones. Exceptions:
  an explicit `recurrence_type` change still re-specifies the whole rule, and
  explicitly sending `recurrence_count` or `recurrence_until` evicts the
  other stored bound (RFC 5545 mutual exclusivity) instead of raising.

## [0.3.2] — 2026-07-11

### Changed
- Repinned `stapel-core` to the `>=0.10,<0.11` window (the published 0.10.0;
  the old `>=0.8,<0.9` pin no longer resolved against PyPI). No code changes.

## [0.3.1] — 2026-07-10

### Fixed
- Re-release of 0.3.0: its publish gate failed on CI missing stapel-tools
  (contract-emission dependency); no code changes beyond the CI fix.

## [Unreleased]

## [0.4.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor lagged behind, so a consumer resolving an older core
regenerated an artifact without `owner` and the drift gate went red — the
field was declared but never required. The floor now matches the artifact
that is committed.

## [0.3.4] — 2026-07-16

### Changed
- **v1 canon sweep §60** (api-versioning.md §2, §6): URL set moved to
  `urls_v1.py`; the new root `urls.py` mounts it under `api/v1/` (the `api/`
  segment historically lives inside this package, so the version slots in
  right after it, per canon). Host mount `calendar/` unchanged: endpoints now
  serve at `/calendar/api/v1/...`; bare `/calendar/api/...` no longer exists
  (sweep lands before the §3 API00x gates are enabled).
- Contract artifacts regenerated (`make contract`): `/v1/` in schema paths.
- `_capabilities.py` canonical_prefix → `/calendar/api/v1`.
- Lint hygiene to a clean `stapel-verify`: explicit `# noqa` on pre-existing
  findings.

## [0.3.0] - 2026-07-09

### Added — event update surface, post-create participant management, visibility axis

Three additive surfaces (contract grows: 8 → 10 operations, first CTO-facing
config axis). Pre-1.0 minor bump.

- **`PATCH /calendar/api/events/{id}`** — partial update of an event
  (owner-only; 403 for a non-owner, 404 out of scope). Only the fields present
  in the body change. Editing any recurrence input (or `start`, the series
  anchor) of a **series master** rebuilds its canonical RRULE symmetrically to
  create — send the complete recurrence spec (the library stores only the
  RRULE, not its inputs, so recurrence params are re-specified, not merged).
  **Rebuild reconciles materialized occurrence exceptions** against the new
  rule by their `recurrence_id`: a child on a still-valid instant is kept
  untouched; an orphaned CANCELLED tombstone is deleted (an EXDATE for a
  vanished instant is meaningless); an orphaned child that carries real state
  (a reschedule, RSVPs, an attached resource) is detached into a standalone
  event — a rule edit never silently destroys user/app data.
- **`PUT /calendar/api/events/{id}/participants`** — replace-set management of
  the invitee list (owner/organizer-only; 403/404 as above). The resulting set
  is `{owner} ∪ participant_ids`: the owner is always retained as accepted,
  ids that persist keep their existing RSVP, new ids are added as invited,
  absent ids are removed.
- **`VISIBILITY` config axis** (participants | scope; capability-config.md §16)
  — the module's first CTO-facing axis, surfaced in `docs/capabilities.json`.
  `participants` (default, fail-closed) keeps the historical private-calendar
  read model; `scope` makes events visible to the whole scope the
  `SCOPE_PROVIDER` resolves (workspace/org/tenant-wide calendars). An unknown
  value degrades to `participants` rather than widening visibility. Behavioral,
  not gating — it changes what the listing/calendar/detail endpoints return,
  not which endpoints exist.
- Contract artifacts (`docs/{schema,capabilities}.json`) regenerated.

## [0.2.3] - 2026-07-09

### Added — `docs/capabilities.json`, the fourth contract artifact (A6 sweep)

Emits `docs/capabilities.json` alongside the schema/flows/errors triad below —
same per-module contract-emission harness, extended to also declare the
module's capability surface for the A6 capabilities mechanism. Enforces
Python 3.12 for emission (rendering-skew guard, keeps the artifact
byte-stable across contributor machines).

### Added — per-module contract emission: `schema` + `flows` + `errors` triad (contract-pipeline.md Wave 1)

stapel-calendar now emits its **own** API contract per-module —
`docs/{schema,flows,errors}.json` — so the frontend codegen (and client
migrations to `stapel-calendar`, §17 W1) can read a committed,
version-pinned artifact instead of checking out a floating-`main` aggregate
(contract-pipeline.md verdict **A**: contract = a reviewable commit). Copied
from stapel-auth's reference implementation (contract-pipeline.md §2-3,
ETALON) and stapel-profiles' adaptation for a no-sibling-co-mount module.

- **Harness** (reuses `stapel_tools.codegen`, adds per-module config):
  - `_codegen_settings.py` — single source of truth for the
    `settings.configure` block, shared with `conftest.py` (extracted, no
    test-behavior change); adds `drf_spectacular` +
    `stapel_core.django.apps.CommonDjangoConfig` to `INSTALLED_APPS`
    (unconditionally — the management commands the errors gate needs) and a
    `contract=True` mode that swaps in the production `REST_FRAMEWORK`.
  - `codegen_urls.py` — mounts `stapel_calendar.urls` alone at the canonical
    `calendar/api/` prefix (the module's own `urls.py` docstring already
    documents this as the expected host mount), so emitted paths are
    `/calendar/api/...` not bare `/api/events`.
  - `_codegen.py` — the `python -m stapel_calendar._codegen --out docs`
    entrypoint. Explicitly registers drf-spectacular's
    `JWTCookieAuthenticationExtension`
    (`stapel_core...swagger._register_jwt_auth_extension`) — calendar has no
    co-mounted sibling to trigger this registration as a side effect (unlike
    auth+gdpr), so without the explicit call every protected operation (all
    six calendar views require `IsAuthenticated`) would silently emit with no
    `security` entry at all.
- **`docs/schema.json`** (new) — drf-spectacular OpenAPI for calendar only,
  canonical `/calendar/api/` prefix, 6 paths, 8-component closure.
  **`docs/flows.json`** (new) — empty array, calendar has no `@flow_step`
  annotations. **`docs/errors.json`** (new) — 48 error keys (7
  calendar-owned + the cross-cutting core registry).
- **Validation:** stapel-calendar is **not yet mounted in
  stapel-example-monolith** (grep-confirmed — no monolith `urls.py`
  references `stapel_calendar`), so there is no aggregate slice to diff
  byte-for-byte (contract-pipeline.md §9 fallback applies). Standalone gates
  substitute: emission determinism (two independent runs byte-identical),
  self-contained `$ref` closure (zero dangling component references — no
  sibling-module dependency needed), every protected operation carries
  `security: [{"JWTCookieAuth": []}]`, and all paths/flow-endpoints carry the
  canonical `/calendar/api/` prefix. A dormant
  `test_matches_monolith_calendar_slice` (unconditionally skipped) is wired
  for the day calendar *is* mounted there, mirroring auth/profiles.
- **Gate:** `make contract` / `make contract-check`; `tests/test_contract.py`
  (drift + determinism + canonical-prefix + `$ref` closure + JWT-security,
  the monolith-identity test dormant until calendar is mounted).

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
