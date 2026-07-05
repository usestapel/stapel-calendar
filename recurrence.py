"""Recurrence engine — RFC 5545 (iCalendar) RRULE as the canonical spec.

This replaces legacy's buggy ad-hoc engine
(``calendar_app/services.py``), whose smells were:

- ``monthly`` implemented as ``timedelta(days=30)`` — drifts off the
  day-of-month and desyncs over a year. Here ``monthly`` is
  ``FREQ=MONTHLY`` via :mod:`dateutil.rrule`, which is calendar-correct:
  a series anchored on Jan 31 yields Mar 31, May 31, ... (RFC 5545 skips
  months with no 31st) rather than sliding into the wrong day.
- eager materialization of every occurrence — here expansion is **virtual**
  (computed, not persisted) and materialization is **on-demand** (only when
  an occurrence gains its own state).
- ``recurrence_days`` as a raw CSV string — here custom weekdays normalize
  to an RRULE ``BYDAY`` list.

Presets are an **open registry** merged over the built-ins
(``BUILTIN_PRESETS``) via ``STAPEL_CALENDAR["PRESETS"]`` and the runtime
``register_preset()`` API — the fourth extension seam (custom recurrence
rules beyond the built-ins). Setting a name to ``None`` removes a built-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, rrule, rrulestr

# 0=Mon..6=Sun -> RFC 5545 BYDAY tokens.
_BYDAY_TOKENS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_FREQ_TOKENS = {DAILY: "DAILY", WEEKLY: "WEEKLY", MONTHLY: "MONTHLY"}


class InvalidRecurrence(ValueError):
    """Raised when a preset/RRULE cannot be built or parsed."""


def as_utc(dt: datetime) -> datetime:
    """Normalize an aware datetime to UTC; return a naive one unchanged.

    Used everywhere occurrence instants are *compared* (dedup keys,
    rule-membership checks). PEP 495 makes ``==``/``hash`` of inter-zone
    aware datetimes return unequal for gap/ambiguous wall times, so raw
    dict lookups mixing DB-UTC and ZoneInfo-local keys silently miss on
    DST-transition instants — normalizing both sides to UTC restores
    instant semantics.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc)


def add_duration(start: datetime, duration: timedelta) -> datetime:
    """``start + duration`` in *instant* (UTC) space, not wall-clock.

    Naive/UTC datetimes are unaffected. For a ZoneInfo-aware ``start``,
    plain ``start + duration`` is wall-clock arithmetic: across a DST gap
    it can produce ``end < start`` (spring-forward: start resolves with
    the pre-transition offset, end with the post-transition one) and
    across a fold it inflates the duration (fall-back). Doing the addition
    on the UTC instant keeps the duration exact, then converts back to the
    original timezone for display.
    """
    if start.tzinfo is None:
        return start + duration
    return (start.astimezone(timezone.utc) + duration).astimezone(start.tzinfo)


# ── Preset registry (extension seam #4) ─────────────────────────────────
#
# Each preset is a dict of base dateutil.rrule kwargs. Runtime params
# (interval / byweekday / until / count) are merged over the base by
# build_rrule(). ``byweekday`` as a list of ints (0=Mon..6=Sun) is
# normalized to BYDAY tokens.

BUILTIN_PRESETS: dict[str, dict | None] = {
    "none": None,
    "daily": {"freq": DAILY},
    "weekdays": {"freq": DAILY, "byweekday": [0, 1, 2, 3, 4]},
    "weekly": {"freq": WEEKLY},
    "biweekly": {"freq": WEEKLY, "interval": 2},
    # Calendar-correct monthly: same day-of-month, short months skipped.
    "monthly": {"freq": MONTHLY},
    # `custom` carries no base weekdays — caller must pass byweekday.
    "custom": {"freq": WEEKLY},
}

_runtime_presets: dict[str, dict | None] = {}


def register_preset(name: str, spec: dict | None) -> None:
    """Register/override a recurrence preset at runtime. ``spec=None``
    removes a built-in. ``spec`` is a dict of dateutil.rrule kwargs."""
    _runtime_presets[name] = spec


def reset_presets() -> None:
    """Tests only: drop runtime preset overrides."""
    _runtime_presets.clear()


def get_presets() -> dict[str, dict | None]:
    """Effective preset map: built-ins <- settings PRESETS <- runtime, with
    ``None`` values removing a name."""
    from .conf import calendar_settings

    merged: dict[str, dict | None] = dict(BUILTIN_PRESETS)
    for source in (calendar_settings.PRESETS or {}, _runtime_presets):
        for name, spec in source.items():
            merged[name] = spec
    return {name: spec for name, spec in merged.items() if spec is not None}


def normalize_weekdays(weekdays) -> list[int]:
    """Coerce a CSV string / iterable of ints into a sorted unique list of
    0=Mon..6=Sun ints. Replaces legacy's raw ``recurrence_days`` CSV."""
    if weekdays is None:
        return []
    if isinstance(weekdays, str):
        parts = [p.strip() for p in weekdays.split(",") if p.strip()]
        values = [int(p) for p in parts]
    else:
        values = [int(w) for w in weekdays]
    for v in values:
        if not 0 <= v <= 6:
            raise InvalidRecurrence(f"weekday out of range 0..6: {v}")
    return sorted(set(values))


def _byday_tokens(weekdays: list[int]) -> str:
    return ",".join(_BYDAY_TOKENS[w] for w in weekdays)


def build_rrule(
    preset: str,
    *,
    interval: int | None = None,
    byweekday=None,
    until: datetime | None = None,
    count: int | None = None,
    dtstart: datetime | None = None,
) -> str:
    """Build a canonical RRULE string (no DTSTART) for ``preset``.

    Returns ``""`` for the ``none`` preset. Raises :class:`InvalidRecurrence`
    for unknown presets, a ``custom`` preset without weekdays, or ``count``
    and ``until`` together (RFC 5545 §3.3.10 forbids both in one rule).

    ``dtstart`` (the series start) is the timezone context for ``until``:

    - aware ``until`` — converted to UTC (``Z`` form). Requires an aware
      ``dtstart`` if one is given: dateutil rejects a UTC UNTIL against a
      naive DTSTART at *expand* time, which would poison every later
      expansion of the stored series — so it is caught here, at build time.
    - naive ``until`` with an aware ``dtstart`` — interpreted in the
      series' timezone, then converted to UTC (not blindly stamped ``Z``).
    - naive ``until`` with a naive ``dtstart`` — kept naive (wall-clock
      host, RFC "floating" time).
    - naive ``until`` without ``dtstart`` — rejected: no timezone context
      to interpret it in.
    """
    presets = get_presets()
    if preset == "none":
        return ""
    if preset not in presets:
        raise InvalidRecurrence(f"unknown recurrence preset: {preset!r}")
    if count is not None and until is not None:
        raise InvalidRecurrence(
            "count and until are mutually exclusive (RFC 5545 §3.3.10)"
        )

    base = dict(presets[preset])
    freq = base.get("freq", WEEKLY)

    wd = byweekday if byweekday is not None else base.get("byweekday")
    weekdays = normalize_weekdays(wd) if wd is not None else []
    if preset == "custom" and not weekdays:
        raise InvalidRecurrence("custom recurrence requires weekdays")

    eff_interval = interval if interval is not None else base.get("interval", 1)
    if eff_interval < 1:
        raise InvalidRecurrence("interval must be >= 1")

    parts = [f"FREQ={_FREQ_TOKENS[freq]}"]
    if eff_interval != 1:
        parts.append(f"INTERVAL={eff_interval}")
    if weekdays:
        parts.append(f"BYDAY={_byday_tokens(weekdays)}")
    if count is not None:
        parts.append(f"COUNT={int(count)}")
    if until is not None:
        parts.append(f"UNTIL={_format_until(until, dtstart)}")
    return ";".join(parts)


def _format_until(until: datetime, dtstart: datetime | None) -> str:
    """Render UNTIL per RFC 5545, using ``dtstart`` as timezone context."""
    dtstart_naive = dtstart is not None and dtstart.tzinfo is None
    if until.tzinfo is None:
        if dtstart is None:
            raise InvalidRecurrence(
                "naive until requires dtstart for timezone context"
            )
        if dtstart_naive:
            # Floating-time host: keep UNTIL floating too (no Z).
            return until.strftime("%Y%m%dT%H%M%S")
        # Interpret the naive until in the series' timezone, then to UTC.
        until = until.replace(tzinfo=dtstart.tzinfo)
    elif dtstart_naive:
        # dateutil raises "RRULE UNTIL values must be specified in UTC when
        # DTSTART is timezone-aware" (and the naive-DTSTART converse) only
        # at expand time — reject at build time instead of storing a series
        # that 500s every later expansion.
        raise InvalidRecurrence(
            "aware until with a naive event start: make both naive or both aware"
        )
    return until.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_rrule(rule_text: str, dtstart: datetime) -> rrule:
    """Parse a canonical RRULE line anchored at ``dtstart`` into a
    :class:`dateutil.rrule.rrule`. Raises :class:`InvalidRecurrence`."""
    if not rule_text:
        raise InvalidRecurrence("empty RRULE")
    try:
        # rrulestr accepts a bare "FREQ=..." line as well as "RRULE:FREQ=...".
        return rrulestr(rule_text, dtstart=dtstart)
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise InvalidRecurrence(str(exc)) from exc


@dataclass(frozen=True)
class Occurrence:
    """A single instance of a (possibly recurring) event over a range.

    ``event_id`` is the *series master* id. ``materialized_id`` is set iff a
    concrete child row exists for this instant (its own RSVPs/resource);
    otherwise the occurrence is virtual (never persisted).
    """

    event_id: object
    start: datetime
    end: datetime
    is_materialized: bool = False
    materialized_id: object = None


@dataclass(frozen=True)
class Expansion:
    """Result of a virtual expansion: the occurrences plus a ``truncated``
    flag — True when the ``max_occurrences`` cap cut the expansion short of
    the requested range end (instants past the cap exist but were not
    computed). Callers doing availability math must surface this: a silently
    capped expansion makes everything past the cap look free."""

    occurrences: list[Occurrence] = field(default_factory=list)
    truncated: bool = False


def expand_rule_detailed(
    rule_text: str,
    dtstart: datetime,
    duration: timedelta,
    range_start: datetime,
    range_end: datetime,
    *,
    event_id=None,
    max_occurrences: int | None = None,
) -> Expansion:
    """Virtually expand an RRULE across ``[range_start, range_end]``.

    Returns occurrences whose start falls in the window (inclusive), capped
    at ``max_occurrences`` (defaults to the ``MAX_EXPANSION_OCCURRENCES``
    setting), plus a ``truncated`` flag when the cap was hit with more
    in-range instants remaining. Nothing is persisted.

    Occurrence ends are computed in instant (UTC) space via
    :func:`add_duration` — wall-clock arithmetic across a DST gap produced
    inverted intervals (``end < start``) and inflated ones across a fold.

    Iterates lazily via ``rrule.xafter`` and stops at the cap or the range
    end — so an unbounded rule (e.g. ``FREQ=DAILY`` with no UNTIL/COUNT) over
    a huge range never computes more than ``max_occurrences`` instants. The
    cap is a real work bound, not a post-hoc trim.
    """
    from .conf import calendar_settings

    if max_occurrences is None:
        max_occurrences = calendar_settings.MAX_EXPANSION_OCCURRENCES

    rule = parse_rrule(rule_text, dtstart)
    out: list[Occurrence] = []
    truncated = False
    iterator = rule.xafter(range_start, inc=True)
    for occ_start in iterator:
        if occ_start > range_end:
            break
        out.append(
            Occurrence(
                event_id=event_id,
                start=occ_start,
                end=add_duration(occ_start, duration),
            )
        )
        if len(out) >= max_occurrences:
            nxt = next(iterator, None)
            truncated = nxt is not None and nxt <= range_end
            break
    return Expansion(occurrences=out, truncated=truncated)


def expand_rule(
    rule_text: str,
    dtstart: datetime,
    duration: timedelta,
    range_start: datetime,
    range_end: datetime,
    *,
    event_id=None,
    max_occurrences: int | None = None,
) -> list[Occurrence]:
    """List-only variant of :func:`expand_rule_detailed` (drops the
    ``truncated`` flag)."""
    return expand_rule_detailed(
        rule_text,
        dtstart,
        duration,
        range_start,
        range_end,
        event_id=event_id,
        max_occurrences=max_occurrences,
    ).occurrences


def is_rule_instant(rule_text: str, dtstart: datetime, instant: datetime) -> bool:
    """True iff ``instant`` is an occurrence instant of the rule (compared
    in UTC space, so gap/ambiguous DST wall times still match — see
    :func:`as_utc`). Instants past UNTIL/COUNT are not instants."""
    rule = parse_rrule(rule_text, dtstart)
    try:
        candidate = next(rule.xafter(instant, inc=True), None)
    except TypeError as exc:
        # naive/aware mix between the series start and the queried instant.
        raise InvalidRecurrence(str(exc)) from exc
    return candidate is not None and as_utc(candidate) == as_utc(instant)
