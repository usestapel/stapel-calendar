"""ICS (RFC 5545) export — VCALENDAR / VEVENT serialization.

Standard-format export so events open in Google/Apple/Outlook calendars and
so series carry their RRULE. A minimal parser (:func:`parse_ics`) reads the
same fields back, giving an export -> import round-trip for tests and simple
sync consumers.
"""
from __future__ import annotations

from datetime import datetime, timezone

PRODID = "-//Stapel//stapel-calendar//EN"

_ESCAPES = {"\\": "\\\\", ";": "\\;", ",": "\\,", "\n": "\\n"}
_UNESCAPES = {"\\\\": "\\", "\\;": ";", "\\,": ",", "\\n": "\n", "\\N": "\n"}


def _escape(text: str) -> str:
    out = []
    for ch in text or "":
        out.append(_ESCAPES.get(ch, ch))
    return "".join(out)


def _unescape(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        pair = text[i : i + 2]
        if pair in _UNESCAPES:
            out.append(_UNESCAPES[pair])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _fmt_dt(dt: datetime) -> str:
    """UTC basic format with Z suffix (RFC 5545 UTC form)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _parse_dt(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y%m%dT%H%M%S")


def event_to_vevent(event) -> list[str]:
    """Serialize one Event model to VEVENT lines. A series master emits its
    RRULE; concrete events omit it."""
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.id}",
        f"DTSTAMP:{_fmt_dt(event.created_at)}",
        f"DTSTART:{_fmt_dt(event.start)}",
        f"DTEND:{_fmt_dt(event.end)}",
        f"SUMMARY:{_escape(event.title)}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    if event.rrule:
        lines.append(f"RRULE:{event.rrule}")
    lines.append(f"STATUS:{event.status.upper()}")
    lines.append("END:VEVENT")
    return lines


def to_ics(events) -> str:
    """Wrap events in a VCALENDAR. Accepts a single Event or an iterable."""
    if hasattr(events, "id"):
        events = [events]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(event_to_vevent(event))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def parse_ics(text: str) -> list[dict]:
    """Parse VEVENT blocks back into dicts (uid/summary/description/start/
    end/rrule/status). Minimal — enough for a round-trip and simple sync."""
    events: list[dict] = []
    current: dict | None = None
    # Unfold RFC 5545 line continuations (a line starting with space/tab is
    # a continuation of the previous).
    raw_lines = text.replace("\r\n", "\n").split("\n")
    unfolded: list[str] = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.split(";", 1)[0].upper()
        if name == "UID":
            current["uid"] = value
        elif name == "SUMMARY":
            current["summary"] = _unescape(value)
        elif name == "DESCRIPTION":
            current["description"] = _unescape(value)
        elif name == "DTSTART":
            current["start"] = _parse_dt(value)
        elif name == "DTEND":
            current["end"] = _parse_dt(value)
        elif name == "RRULE":
            current["rrule"] = value
        elif name == "STATUS":
            current["status"] = value.lower()
    return events
