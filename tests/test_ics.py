"""ICS (RFC 5545) export + round-trip."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar import ics, services

UTC = ZoneInfo("UTC")


@pytest.mark.django_db
class TestICS:
    def test_export_roundtrip(self, user):
        ev = services.create_event(
            owner=user,
            title="Design; review, part 1",
            description="Line one\nLine two",
            start=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        )
        text = ics.to_ics(ev)
        assert "BEGIN:VCALENDAR" in text
        assert "BEGIN:VEVENT" in text

        parsed = ics.parse_ics(text)
        assert len(parsed) == 1
        row = parsed[0]
        assert row["uid"] == str(ev.id)
        # Escaped special chars round-trip.
        assert row["summary"] == "Design; review, part 1"
        assert row["description"] == "Line one\nLine two"
        assert row["start"] == ev.start
        assert row["end"] == ev.end

    def test_series_carries_rrule(self, user):
        ev = services.create_event(
            owner=user,
            title="Weekly sync",
            start=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            recurrence_type="weekly",
        )
        parsed = ics.parse_ics(ics.to_ics(ev))
        assert parsed[0]["rrule"] == "FREQ=WEEKLY"

    def test_multiple_events(self, user):
        e1 = services.create_event(
            owner=user, title="A",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        )
        e2 = services.create_event(
            owner=user, title="B",
            start=datetime(2026, 1, 6, 9, tzinfo=UTC),
            end=datetime(2026, 1, 6, 10, tzinfo=UTC),
        )
        parsed = ics.parse_ics(ics.to_ics([e1, e2]))
        assert {p["summary"] for p in parsed} == {"A", "B"}


def _unfold(folded: str) -> str:
    """RFC 5545 unfolding: drop each CRLF and the single space after it."""
    return folded.replace("\r\n ", "")


class TestTextIsNeverStructure:
    """Security audit 2026-09-11, L-7.

    A calendar entry's title is attacker-supplied text on every surface that
    lets somebody invite you. `_ESCAPES` covered backslash, semicolon, comma
    and LF and not CR, so a title carrying a bare carriage return reached a
    CR-splitting parser as a NEW PROPERTY: `\rATTENDEE:mallory@example.net`
    adds an attendee, `\rORGANIZER:` moves the organizer, `\rURL:` adds a
    link. RFC 5545 line endings are CRLF, and plenty of readers split on
    either half.
    """

    def test_a_carriage_return_in_a_title_stays_text(self):
        line = ics._escape("Standup\r\nATTENDEE:mallory@example.net")
        assert "\r" not in line
        assert "\n" not in line
        assert line == "Standup\\nATTENDEE:mallory@example.net"

    def test_a_lone_carriage_return_stays_text(self):
        assert ics._escape("a\rb") == "a\\nb"

    def test_a_folded_long_line_unfolds_to_exactly_what_went_in(self):
        """RFC 5545 caps a content line at 75 octets; a reader that meets a
        longer one is free to truncate it or give up on the file."""
        content = f"SUMMARY:{ics._escape('Quarterly planning ' + 'long title ' * 20)}"
        folded = ics._fold(content)
        for physical in folded.split("\r\n"):
            assert len(physical.encode()) <= 75, physical
        assert _unfold(folded) == content

    def test_folding_never_splits_a_character(self):
        content = f"SUMMARY:{ics._escape('Планирование ' * 12)}"
        folded = ics._fold(content)
        for physical in folded.split("\r\n"):
            physical.encode().decode()  # would raise on a split code point
            assert len(physical.encode()) <= 75
        assert _unfold(folded) == content

    def test_a_short_line_is_left_alone(self):
        assert ics._fold("SUMMARY:hi") == "SUMMARY:hi"


@pytest.mark.django_db
class TestInjectedPropertiesAreJustText:
    def test_a_title_carrying_a_property_round_trips_as_the_title(self, user):
        hostile = "Standup\r\nATTENDEE:mallory@example.net"
        ev = services.create_event(
            owner=user,
            title=hostile,
            start=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        )
        text = ics.to_ics(ev)
        # The injected name must never begin a content line — which is the
        # only place a reader looks for a property.
        assert not any(
            line.startswith("ATTENDEE") for line in text.split("\r\n")
        ), text
        parsed = ics.parse_ics(text)
        assert len(parsed) == 1
        assert "attendee" not in parsed[0]
        assert parsed[0]["summary"] == "Standup\nATTENDEE:mallory@example.net"

    def test_a_long_title_survives_the_round_trip(self, user):
        title = "Planning " + "x" * 300
        ev = services.create_event(
            owner=user,
            title=title,
            start=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        )
        text = ics.to_ics(ev)
        for physical in text.split("\r\n"):
            assert len(physical.encode()) <= 75, physical
        assert ics.parse_ics(text)[0]["summary"] == title
