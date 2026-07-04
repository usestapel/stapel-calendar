"""comm surface — Function call + emit schema validation (VALIDATE_SCHEMAS on)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from stapel_core.comm import call

from stapel_calendar import services

UTC = ZoneInfo("UTC")


@pytest.mark.django_db
class TestFreeBusyFunction:
    def test_call_in_process(self, user):
        services.create_event(
            owner=user, title="Busy",
            start=datetime(2026, 1, 5, 10, tzinfo=UTC),
            end=datetime(2026, 1, 5, 11, tzinfo=UTC),
        )
        result = call(
            "calendar.free_busy",
            {
                "user_id": str(user.id),
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-31T00:00:00+00:00",
            },
        )
        assert len(result["busy"]) == 1

    def test_bad_payload_rejected_by_schema(self, user):
        # `end` missing -> schema validation (required) must reject.
        with pytest.raises(Exception):
            call("calendar.free_busy", {"user_id": str(user.id)})


@pytest.mark.django_db
class TestEmitSchemas:
    def test_materialized_emit_passes_schema(self, user, captured_events):
        series = services.create_event(
            owner=user, title="Weekly",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            recurrence_type="weekly",
        )
        # If the payload drifted from schemas/emits/, emit() would raise here.
        services.materialize(series, datetime(2026, 1, 12, 9, tzinfo=UTC))
        assert any(
            e.event_type == "calendar.occurrence.materialized" for e in captured_events
        )

    def test_reminder_emit_passes_schema(self, user, captured_events, settings):
        from django.utils import timezone

        from stapel_calendar.reminders import run_reminders

        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [10]}
        now = timezone.now()
        services.create_event(
            owner=user, title="Soon",
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=40),
        )
        run_reminders(now)
        assert any(
            e.event_type == "calendar.event.reminder_due" for e in captured_events
        )
