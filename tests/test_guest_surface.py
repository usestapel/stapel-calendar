"""The guest (anonymous session) surface of stapel-calendar.

With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a
bare ``IsAuthenticated`` gate lets it through, and until now nothing in the
source said whether that was wanted (``stapel_core.adoption`` W002).
``views.py`` now states it per view; this module keeps the statement honest.

The half that would hurt in production is the open half: a real consumer
(meettoday) renders the guest's one and only page with
``GET /calendar/api/v1/events`` on it. A regression that closes that view
turns a guest's landing page into an error — in production, not in review.
The first test below is the one that catches it.
"""

import pytest

from stapel_calendar import views
from stapel_calendar.models import Event, Participant, RSVP
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    IsNotAnonymousUser,
)

RANGE = {"start": "2026-01-01T00:00:00+00:00", "end": "2026-12-31T00:00:00+00:00"}


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints:
    authenticated, ``is_anonymous=True``."""
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def guest_client(api_client, guest):
    api_client.force_authenticate(user=guest)
    return api_client


@pytest.fixture
def someone_elses_event(user):
    """An event a guest has nothing to do with."""
    from stapel_calendar import services

    return services.create_event(
        owner=user,
        title="Board meeting",
        start="2026-03-02T09:00:00+00:00",
        end="2026-03-02T10:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# The guest path: an empty calendar, not a refusal
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestReadsAnEmptyCalendar:
    def test_event_list_is_empty_not_forbidden(self, guest_client, someone_elses_event):
        resp = guest_client.get("/calendar/api/v1/events", RANGE)
        assert resp.status_code == 200, resp.content
        assert resp.json() == []

    def test_calendar_view_is_empty_not_forbidden(
        self, guest_client, someone_elses_event
    ):
        resp = guest_client.get("/calendar/api/v1/calendar", RANGE)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["events"] == []
        assert body["occurrences"] == []

    def test_availability_reports_the_guest_entirely_free(self, guest_client):
        resp = guest_client.get("/calendar/api/v1/availability", RANGE)
        assert resp.status_code == 200, resp.content
        assert resp.json()["busy"] == []

    def test_declared_allowed_in_the_source(self):
        for view in (
            views.EventListCreateView,
            views.CalendarView,
            views.AvailabilityView,
        ):
            assert view.stapel_anonymous_access == ANONYMOUS_ALLOWED, view.__name__


# ---------------------------------------------------------------------------
# A guest may not open one named event
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestCannotOpenAnEvent:
    def test_detail(self, guest_client, someone_elses_event):
        resp = guest_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}")
        assert resp.status_code == 403, resp.content

    def test_ics_export(self, guest_client, someone_elses_event):
        resp = guest_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}/ics")
        assert resp.status_code == 403, resp.content

    def test_the_owner_still_can(self, auth_client, someone_elses_event):
        """The gate is about *anonymous*, not about *authenticated*."""
        assert (
            auth_client.get(
                f"/calendar/api/v1/events/{someone_elses_event.id}"
            ).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# A guest may not write
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestCannotWrite:
    def test_create_event(self, guest_client):
        resp = guest_client.post(
            "/calendar/api/v1/events",
            {
                "title": "Ghost standup",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T09:15:00+00:00",
            },
            format="json",
        )
        assert resp.status_code == 403, resp.content
        assert not Event.objects.exists()

    def test_create_recurring_event(self, guest_client):
        """A series is the worst case: it keeps materializing occurrences long
        after the session that made it is gone."""
        resp = guest_client.post(
            "/calendar/api/v1/events",
            {
                "title": "Forever",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T09:15:00+00:00",
                "recurrence_type": "weekdays",
            },
            format="json",
        )
        assert resp.status_code == 403, resp.content
        assert not Event.objects.exists()

    def test_registered_user_still_creates(self, auth_client):
        resp = auth_client.post(
            "/calendar/api/v1/events",
            {
                "title": "Kickoff",
                "start": "2026-01-05T09:00:00+00:00",
                "end": "2026-01-05T10:00:00+00:00",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.content

    def test_patch_event(self, guest_client, someone_elses_event):
        resp = guest_client.patch(
            f"/calendar/api/v1/events/{someone_elses_event.id}",
            {"title": "Hijacked"},
            format="json",
        )
        assert resp.status_code == 403, resp.content
        someone_elses_event.refresh_from_db()
        assert someone_elses_event.title == "Board meeting"

    def test_delete_event(self, guest_client, someone_elses_event):
        resp = guest_client.delete(
            f"/calendar/api/v1/events/{someone_elses_event.id}"
        )
        assert resp.status_code == 403, resp.content
        assert Event.objects.filter(id=someone_elses_event.id).exists()

    def test_replace_participants(self, guest_client, someone_elses_event, guest):
        resp = guest_client.put(
            f"/calendar/api/v1/events/{someone_elses_event.id}/participants",
            {"participant_ids": [str(guest.id)]},
            format="json",
        )
        assert resp.status_code == 403, resp.content
        assert not Participant.objects.filter(user_id=guest.id).exists()

    def test_rsvp(self, guest_client, someone_elses_event, guest):
        Participant.objects.create(
            event=someone_elses_event, user_id=guest.id, rsvp=RSVP.INVITED
        )
        resp = guest_client.post(
            f"/calendar/api/v1/events/{someone_elses_event.id}/respond",
            {"rsvp": RSVP.ACCEPTED},
            format="json",
        )
        assert resp.status_code == 403, resp.content
        assert (
            Participant.objects.get(
                event=someone_elses_event, user_id=guest.id
            ).rsvp
            == RSVP.INVITED
        )


def test_closed_views_carry_the_permission_class():
    """Two closed for anonymity alone (owner-only / invitee-only downstream),
    two closed for the third state as well — see tests/test_mandate_surface.py
    for why ``IsNotAnonymousUser`` was not enough on the by-id reads."""
    from stapel_core.django.api.permissions import HasWorkspaceMandateIfScoped

    for view in (views.EventParticipantsView, views.EventRespondView):
        assert IsNotAnonymousUser in view.permission_classes, view.__name__
    for view in (views.EventDetailView, views.EventICSView):
        assert HasWorkspaceMandateIfScoped in view.permission_classes, view.__name__


def test_no_view_is_left_silent():
    """The question ``stapel_core.adoption`` E001/W002 asks a consumer's
    deployment, asked here — where it can be answered."""
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.views import APIView

    from stapel_core.django.api.permissions import ANONYMOUS_DECLARATIONS

    silent = [
        name
        for name, obj in vars(views).items()
        if isinstance(obj, type)
        and issubclass(obj, APIView)
        and set(getattr(obj, "permission_classes", ()) or ()) == {IsAuthenticated}
        and getattr(obj, "stapel_anonymous_access", None) not in ANONYMOUS_DECLARATIONS
    ]
    assert silent == []
