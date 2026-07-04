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

from dataclasses import dataclass
from datetime import datetime, timedelta

from dateutil.rrule import (
    DAILY,
    FR,
    MO,
    MONTHLY,
    TH,
    TU,
    WE,
    WEEKLY,
    rrule,
    rrulestr,
)

# 0=Mon..6=Sun -> dateutil weekday objects (BYDAY tokens).
_WEEKDAY_OBJECTS = (MO, TU, WE, TH, FR)  # index by isoweekday-1 for the first 5
_BYDAY_TOKENS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_FREQ_TOKENS = {DAILY: "DAILY", WEEKLY: "WEEKLY", MONTHLY: "MONTHLY"}


class InvalidRecurrence(ValueError):
    """Raised when a preset/RRULE cannot be built or parsed."""


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
) -> str:
    """Build a canonical RRULE string (no DTSTART) for ``preset``.

    Returns ``""`` for the ``none`` preset. Raises :class:`InvalidRecurrence`
    for unknown presets or a ``custom`` preset without weekdays.
    """
    presets = get_presets()
    if preset == "none":
        return ""
    if preset not in presets:
        raise InvalidRecurrence(f"unknown recurrence preset: {preset!r}")

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
        # RFC 5545 UNTIL in UTC (Z form).
        u = until
        if u.tzinfo is not None:
            from datetime import timezone as _tz

            u = u.astimezone(_tz.utc)
        parts.append(f"UNTIL={u.strftime('%Y%m%dT%H%M%SZ')}")
    return ";".join(parts)


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
    """Virtually expand an RRULE across ``[range_start, range_end]``.

    Returns occurrences whose start falls in the window (inclusive), capped
    at ``max_occurrences`` (defaults to the ``MAX_EXPANSION_OCCURRENCES``
    setting). Nothing is persisted.
    """
    from .conf import calendar_settings

    if max_occurrences is None:
        max_occurrences = calendar_settings.MAX_EXPANSION_OCCURRENCES

    rule = parse_rrule(rule_text, dtstart)
    out: list[Occurrence] = []
    for occ_start in rule.between(range_start, range_end, inc=True):
        out.append(
            Occurrence(
                event_id=event_id,
                start=occ_start,
                end=occ_start + duration,
            )
        )
        if len(out) >= max_occurrences:
            break
    return out
