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
