"""G1 PATCH /events/{id} (partial update + RRULE-rebuild of a series master),
G2 PUT /events/{id}/participants (replace-set, owner-only) and G3 the
VISIBILITY axis (participants|scope, capability-config.md §16)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar import services
from stapel_calendar.models import Event, EventStatus, Participant, RSVP

UTC = ZoneInfo("UTC")


def _dt(day, hour=9):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


# ── G1: PATCH /events/{id} ──────────────────────────────────────────────


@pytest.mark.django_db
class TestUpdateEventAPI:
    def test_patch_simple_fields(self, auth_client, user):
        ev = services.create_event(
            owner=user, title="Old", start=_dt(5), end=_dt(5, 10)
        )
        resp = auth_client.patch(
            f"/calendar/api/events/{ev.id}",
            {"title": "New", "description": "d"},
            format="json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "New"
        assert body["description"] == "d"
        # Untouched fields survive a partial update.
        ev.refresh_from_db()
        assert ev.start == _dt(5)

    def test_patch_only_sends_present_fields(self, auth_client, user):
        """Absent fields are not reset to a default (true PATCH)."""
        ev = services.create_event(
            owner=user, title="Keep", description="orig", start=_dt(5), end=_dt(5, 10)
        )
        resp = auth_client.patch(
            f"/calendar/api/events/{ev.id}", {"title": "Renamed"}, format="json"
        )
        assert resp.status_code == 200
        ev.refresh_from_db()
        assert ev.title == "Renamed"
        assert ev.description == "orig"

    def test_patch_bad_range_rejected(self, auth_client, user):
        ev = services.create_event(
            owner=user, title="E", start=_dt(5), end=_dt(5, 10)
        )
        # New end before existing start.
        resp = auth_client.patch(
            f"/calendar/api/events/{ev.id}",
            {"end": "2026-01-05T08:00:00+00:00"},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.calendar_invalid_range"

    def test_patch_non_owner_forbidden(self, api_client, user, other_user):
        ev = services.create_event(
            owner=user, title="Owned", start=_dt(5), end=_dt(5, 10),
            participant_ids=[str(other_user.id)],
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.patch(
            f"/calendar/api/events/{ev.id}", {"title": "hijack"}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == "error.403.calendar_not_event_owner"

    def test_patch_missing_event_404(self, auth_client):
        resp = auth_client.patch(
            "/calendar/api/events/3fa85f64-5717-4562-b3fc-2c963f66afa6",
            {"title": "x"},
            format="json",
        )
        assert resp.status_code == 404

    def test_patch_rebuilds_rrule(self, auth_client, user):
        ev = services.create_event(
            owner=user, title="Series", start=_dt(5), end=_dt(5, 10),
            recurrence_type="weekly",
        )
        assert ev.rrule == "FREQ=WEEKLY"
        resp = auth_client.patch(
            f"/calendar/api/events/{ev.id}",
            {"recurrence_type": "weekdays"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.json()["rrule"] == "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"

    def test_patch_invalid_recurrence_rejected(self, auth_client, user):
        ev = services.create_event(
            owner=user, title="Series", start=_dt(5), end=_dt(5, 10),
            recurrence_type="weekly",
        )
        resp = auth_client.patch(
            f"/calendar/api/events/{ev.id}",
            {"recurrence_type": "custom"},  # custom needs weekdays
            format="json",
        )
        assert resp.status_code == 400
        assert (
            resp.json()["localizable_error"]
            == "error.400.calendar_invalid_recurrence"
        )


@pytest.mark.django_db
class TestRebuildOccurrenceFate:
    """Semantics of update_event's series rebuild for materialized children."""

    def _weekly_master(self, user):
        # Weekly on Monday (Jan 5, 2026 is a Monday).
        return services.create_event(
            owner=user, title="Weekly", start=_dt(5), end=_dt(5, 10),
            recurrence_type="weekly",
        )

    def test_kept_when_instant_survives(self, user):
        master = self._weekly_master(user)
        occ = services.materialize(master, _dt(12))  # next Monday, still weekly
        # Switch weekly -> weekdays: Monday is still an instant of the new rule.
        services.update_event(master, {"recurrence_type": "weekdays"})
        occ.refresh_from_db()
        assert occ.recurrence_parent_id == master.id
        assert occ.recurrence_id == _dt(12)

    def test_orphaned_tombstone_deleted(self, user):
        master = self._weekly_master(user)
        # Cancel the Tuesday-that-will-exist-only-after-the-switch? Use a
        # Monday tombstone, then move the series to Tuesdays so Monday is gone.
        tomb = services.cancel_occurrence(master, _dt(12))  # Monday tombstone
        assert tomb.status == EventStatus.CANCELLED
        # Rebuild to Tuesdays-only: Monday Jan 12 is no longer an instant.
        services.update_event(
            master,
            {"recurrence_type": "custom", "recurrence_weekdays": [1]},
        )
        assert not Event.objects.filter(id=tomb.id).exists()

    def test_orphaned_real_occurrence_detached(self, user):
        master = self._weekly_master(user)
        occ = services.materialize(master, _dt(12))  # real Monday occurrence
        # Move the series to Tuesdays: Monday Jan 12 is no longer an instant,
        # but the occurrence carries state (participants) -> detach, not delete.
        services.update_event(
            master,
            {"recurrence_type": "custom", "recurrence_weekdays": [1]},
        )
        occ.refresh_from_db()
        assert occ.recurrence_parent_id is None
        assert occ.recurrence_id is None
        # Survives as a standalone event at its own start.
        assert occ.start == _dt(12)

    def test_recurrence_off_orphans_all_children(self, user):
        master = self._weekly_master(user)
        occ = services.materialize(master, _dt(12))
        services.update_event(master, {"recurrence_type": "none"})
        master.refresh_from_db()
        assert master.rrule == ""
        occ.refresh_from_db()
        assert occ.recurrence_parent_id is None


# ── G2: PUT /events/{id}/participants ───────────────────────────────────


@pytest.mark.django_db
class TestReplaceParticipantsAPI:
    def test_replace_set(self, auth_client, user, other_user):
        ev = services.create_event(
            owner=user, title="M", start=_dt(5), end=_dt(5, 10),
            participant_ids=[str(other_user.id)],
        )
        # Replace {owner, other_user} with {owner} only.
        resp = auth_client.put(
            f"/calendar/api/events/{ev.id}/participants",
            {"participant_ids": []},
            format="json",
        )
        assert resp.status_code == 200
        user_ids = {p["user_id"] for p in resp.json()["participants"]}
        assert user_ids == {str(user.id)}  # owner always retained
        assert not Participant.objects.filter(event=ev, user=other_user).exists()

    def test_replace_adds_new_and_keeps_owner_accepted(
        self, auth_client, user, other_user
    ):
        ev = services.create_event(
            owner=user, title="M", start=_dt(5), end=_dt(5, 10)
        )
        resp = auth_client.put(
            f"/calendar/api/events/{ev.id}/participants",
            {"participant_ids": [str(other_user.id)]},
            format="json",
        )
        assert resp.status_code == 200
        by_user = {p["user_id"]: p["rsvp"] for p in resp.json()["participants"]}
        assert by_user[str(user.id)] == "accepted"
        assert by_user[str(other_user.id)] == "invited"

    def test_replace_preserves_existing_rsvp(self, auth_client, user, other_user):
        ev = services.create_event(
            owner=user, title="M", start=_dt(5), end=_dt(5, 10),
            participant_ids=[str(other_user.id)],
        )
        services.respond(ev, other_user, RSVP.ACCEPTED)
        # Re-send a list that still contains other_user — RSVP must survive.
        auth_client.put(
            f"/calendar/api/events/{ev.id}/participants",
            {"participant_ids": [str(other_user.id)]},
            format="json",
        )
        assert (
            Participant.objects.get(event=ev, user=other_user).rsvp == RSVP.ACCEPTED
        )

    def test_replace_non_owner_forbidden(self, api_client, user, other_user):
        ev = services.create_event(
            owner=user, title="M", start=_dt(5), end=_dt(5, 10),
            participant_ids=[str(other_user.id)],
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.put(
            f"/calendar/api/events/{ev.id}/participants",
            {"participant_ids": []},
            format="json",
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == "error.403.calendar_not_event_owner"

    def test_replace_missing_event_404(self, auth_client):
        resp = auth_client.put(
            "/calendar/api/events/3fa85f64-5717-4562-b3fc-2c963f66afa6/participants",
            {"participant_ids": []},
            format="json",
        )
        assert resp.status_code == 404


# ── G3: VISIBILITY axis (participants | scope) ──────────────────────────


@pytest.mark.django_db
class TestVisibilityAxis:
    def test_participants_mode_hides_non_participant_events(
        self, api_client, user, other_user, settings
    ):
        """Default (participants): a user only lists events they are on."""
        settings.STAPEL_CALENDAR = {"VISIBILITY": "participants"}
        services.create_event(
            owner=user, title="Mine", start=_dt(5), end=_dt(5, 10)
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.get(
            "/calendar/api/events?start=2026-01-01T00:00:00Z&end=2026-01-31T00:00:00Z"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_scope_mode_shows_scope_wide_events(
        self, api_client, user, other_user, settings
    ):
        """scope: a non-participant in the same scope sees the event."""
        settings.STAPEL_CALENDAR = {"VISIBILITY": "scope"}
        services.create_event(
            owner=user, title="Shared", start=_dt(5), end=_dt(5, 10)
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.get(
            "/calendar/api/events?start=2026-01-01T00:00:00Z&end=2026-01-31T00:00:00Z"
        )
        assert resp.status_code == 200
        assert [e["title"] for e in resp.json()] == ["Shared"]

    def test_unknown_value_fails_closed_to_participants(
        self, api_client, user, other_user, settings
    ):
        """A typo must not widen visibility — anything != scope is participants."""
        settings.STAPEL_CALENDAR = {"VISIBILITY": "everyone"}
        services.create_event(
            owner=user, title="Mine", start=_dt(5), end=_dt(5, 10)
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.get(
            "/calendar/api/events?start=2026-01-01T00:00:00Z&end=2026-01-31T00:00:00Z"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_scope_mode_calendar_expands_others_series(
        self, api_client, user, other_user, settings
    ):
        """scope visibility also widens the /calendar occurrence expansion."""
        settings.STAPEL_CALENDAR = {"VISIBILITY": "scope"}
        services.create_event(
            owner=user, title="Weekly", start=_dt(5), end=_dt(5, 10),
            recurrence_type="weekly",
        )
        api_client.force_authenticate(user=other_user)
        resp = api_client.get(
            "/calendar/api/calendar?start=2026-01-01T00:00:00Z&end=2026-02-01T00:00:00Z"
        )
        assert resp.status_code == 200
        assert len(resp.json()["occurrences"]) >= 4
