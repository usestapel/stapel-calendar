"""GDPR: user.deleted consumer erases the module's PII, schema-validated."""
import json
import pathlib
import types
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

import jsonschema
import pytest

from stapel_calendar import services
from stapel_calendar.gdpr import CalendarGDPRProvider
from stapel_calendar.models import AvailabilityWindow, Event, Participant

UTC = ZoneInfo("UTC")

CONSUMES_SCHEMA = json.loads(
    (
        pathlib.Path(__file__).resolve().parent.parent
        / "schemas"
        / "consumes"
        / "user.deleted.json"
    ).read_text()
)


def _event(user, title="Ev"):
    return services.create_event(
        owner=user,
        title=title,
        start=datetime(2026, 1, 5, 9, tzinfo=UTC),
        end=datetime(2026, 1, 5, 10, tzinfo=UTC),
    )


@pytest.mark.django_db
class TestGDPRProvider:
    def test_registered_in_registry(self):
        from stapel_core.gdpr import gdpr_registry

        assert "calendar" in gdpr_registry.sections

    def test_export(self, user):
        _event(user)
        data = CalendarGDPRProvider().export(user.id)
        assert len(data["owned_events"]) == 1
        # Owner is auto-added as an accepted participant.
        assert len(data["participations"]) == 1

    def test_delete_erases_owned_and_participations(self, user, other_user):
        _event(user)
        # user is invited to other_user's event.
        other_ev = services.create_event(
            owner=other_user,
            title="Theirs",
            start=datetime(2026, 1, 6, 9, tzinfo=UTC),
            end=datetime(2026, 1, 6, 10, tzinfo=UTC),
            participant_ids=[str(user.id)],
        )
        AvailabilityWindow.objects.create(
            user=user, weekday=0, start_time=time(9), end_time=time(17), timezone="UTC"
        )

        CalendarGDPRProvider().delete(user.id)

        assert not Event.objects.filter(owner_id=user.id).exists()
        assert not Participant.objects.filter(user_id=user.id).exists()
        assert not AvailabilityWindow.objects.filter(user_id=user.id).exists()
        # Other user's event survives (their data), minus the deleted user's RSVP.
        assert Event.objects.filter(id=other_ev.id).exists()
        assert Participant.objects.filter(event=other_ev, user=other_user).exists()

    def test_delete_cascades_occurrences(self, user):
        series = services.create_event(
            owner=user,
            title="Weekly",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
            recurrence_type="weekly",
        )
        services.materialize(series, datetime(2026, 1, 12, 9, tzinfo=UTC))
        assert series.occurrences.count() == 1
        CalendarGDPRProvider().delete(user.id)
        assert Event.objects.count() == 0  # master + occurrence both gone

    def test_anonymize_is_noop(self, user):
        assert CalendarGDPRProvider().anonymize(user.id) is None


@pytest.mark.django_db
class TestUserDeletedAction:
    def test_handler_erases(self, user):
        from stapel_calendar.actions import handle_user_deleted

        _event(user)
        handle_user_deleted(
            types.SimpleNamespace(
                payload={"user_id": str(user.id)}, event_id="evt-1"
            )
        )
        assert not Event.objects.filter(owner_id=user.id).exists()

    def test_handler_without_user_id_logs_and_returns(self, caplog):
        from stapel_calendar.actions import handle_user_deleted

        with caplog.at_level("ERROR", logger="stapel_calendar.actions"):
            handle_user_deleted(types.SimpleNamespace(payload={}, event_id="evt-2"))
        assert any("without user_id" in r.message for r in caplog.records)


class TestConsumesSchema:
    def test_valid_payload_accepted(self):
        jsonschema.validate(
            {"user_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
             "trigger": "manual"},
            CONSUMES_SCHEMA,
        )

    def test_missing_user_id_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"trigger": "manual"}, CONSUMES_SCHEMA)

    def test_bad_trigger_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                {"user_id": str(uuid.uuid4()), "trigger": "nope"}, CONSUMES_SCHEMA
            )
