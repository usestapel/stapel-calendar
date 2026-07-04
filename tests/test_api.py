"""HTTP API: events CRUD, respond (RSVP), user-calendar, availability, ICS."""
from datetime import time

import pytest

from stapel_calendar.models import AvailabilityWindow, Participant, RSVP


@pytest.mark.django_db
class TestEventsAPI:
    def test_create_event(self, auth_client):
        resp = auth_client.post(
            "/calendar/api/events",
            {
                "title": "Kickoff",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T10:00:00+00:00",
            },
            format="json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Kickoff"
        # Owner auto-added as accepted participant.
        assert body["participants"][0]["rsvp"] == "accepted"

    def test_create_recurring_event(self, auth_client):
        resp = auth_client.post(
            "/calendar/api/events",
            {
                "title": "Standup",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T09:15:00+00:00",
                "recurrence_type": "weekdays",
            },
            format="json",
        )
        assert resp.status_code == 201
        assert resp.json()["rrule"] == "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"

    def test_create_invalid_recurrence(self, auth_client):
        resp = auth_client.post(
            "/calendar/api/events",
            {
                "title": "Bad",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T10:00:00+00:00",
                "recurrence_type": "custom",  # no weekdays
            },
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.calendar_invalid_recurrence"

    def test_create_bad_range(self, auth_client):
        resp = auth_client.post(
            "/calendar/api/events",
            {
                "title": "Backwards",
                "start": "2026-01-05T10:00:00+00:00",
                "end": "2026-01-05T09:00:00+00:00",
            },
            format="json",
        )
        assert resp.status_code == 400

    def test_list_events_in_range(self, auth_client):
        auth_client.post(
            "/calendar/api/events",
            {
                "title": "In range",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T10:00:00+00:00",
            },
            format="json",
        )
        resp = auth_client.get(
            "/calendar/api/events?start=2026-01-01T00:00:00Z&end=2026-01-31T00:00:00Z"
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_detail_and_delete(self, auth_client):
        created = auth_client.post(
            "/calendar/api/events",
            {
                "title": "Deleteme",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T10:00:00+00:00",
            },
            format="json",
        ).json()
        eid = created["id"]
        assert auth_client.get(f"/calendar/api/events/{eid}").status_code == 200
        assert auth_client.delete(f"/calendar/api/events/{eid}").status_code == 200
        assert auth_client.get(f"/calendar/api/events/{eid}").status_code == 404

    def test_delete_non_owner_forbidden(self, api_client, user, other_user):
        from stapel_calendar import services
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ev = services.create_event(
            owner=user, title="Owned",
            start=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 5, 10, tzinfo=ZoneInfo("UTC")),
            participant_ids=[str(other_user.id)],
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.delete(f"/calendar/api/events/{ev.id}")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestRespondAPI:
    def test_respond(self, api_client, user, other_user):
        from stapel_calendar import services
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ev = services.create_event(
            owner=user, title="Invite",
            start=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 5, 10, tzinfo=ZoneInfo("UTC")),
            participant_ids=[str(other_user.id)],
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(
            f"/calendar/api/events/{ev.id}/respond", {"rsvp": "accepted"}, format="json"
        )
        assert resp.status_code == 200
        assert Participant.objects.get(event=ev, user=other_user).rsvp == RSVP.ACCEPTED

    def test_respond_invalid_rsvp(self, auth_client, user):
        from stapel_calendar import services
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ev = services.create_event(
            owner=user, title="M",
            start=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 5, 10, tzinfo=ZoneInfo("UTC")),
        )
        resp = auth_client.post(
            f"/calendar/api/events/{ev.id}/respond", {"rsvp": "maybe"}, format="json"
        )
        assert resp.status_code == 400

    def test_respond_uninvited(self, api_client, user, other_user):
        from stapel_calendar import services
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ev = services.create_event(
            owner=user, title="M",
            start=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 5, 10, tzinfo=ZoneInfo("UTC")),
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(
            f"/calendar/api/events/{ev.id}/respond", {"rsvp": "accepted"}, format="json"
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestCalendarAndAvailabilityAPI:
    def test_user_calendar_expands_series(self, auth_client):
        auth_client.post(
            "/calendar/api/events",
            {
                "title": "Weekly",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T09:30:00+00:00",
                "recurrence_type": "weekly",
            },
            format="json",
        )
        resp = auth_client.get(
            "/calendar/api/calendar?start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:00Z"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["occurrences"]) >= 4

    def test_availability(self, auth_client, user):
        AvailabilityWindow.objects.create(
            user=user, weekday=0, start_time=time(9), end_time=time(11), timezone="UTC"
        )
        resp = auth_client.get(
            "/calendar/api/availability?start=2026-01-05T00:00:00Z&end=2026-01-06T00:00:00Z&slot_minutes=30"
        )
        assert resp.status_code == 200
        # Monday 09-11 -> 4 slots, none busy.
        assert len(resp.json()["slots"]) == 4

    def test_ics_export(self, auth_client, user):
        from stapel_calendar import services
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ev = services.create_event(
            owner=user, title="Exported",
            start=datetime(2026, 1, 5, 9, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 5, 10, tzinfo=ZoneInfo("UTC")),
        )
        resp = auth_client.get(f"/calendar/api/events/{ev.id}/ics")
        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("text/calendar")
        assert b"BEGIN:VCALENDAR" in resp.content
