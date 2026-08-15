"""F5 — the two-state fix, defeated by the third state.

``EventDetailView`` and ``EventICSView`` resolve an event by UUID through the
scope provider only, with no participation filter. The source comment reasons
about it carefully and lands on ``IsNotAnonymousUser`` — which returns True
for any real account. The vocabulary had two words, so the fix could only
reach for the stronger of two, and the stronger of two is still not enough:
a REGISTERED account belonging to no workspace at all walks straight through
and reads any event by id, and downloads the ``.ics``.

The gate is now ``HasWorkspaceMandateIfScoped`` — the library-view variant,
so a genuinely single-tenant host (where no mandate exists to hold) keeps
working. Mutation-wise: swap it back to ``IsNotAnonymousUser`` and every test
below fails.
"""
import pytest

from stapel_calendar import views
from stapel_core.comm import function_registry
from stapel_core.django.api.permissions import HasWorkspaceMandateIfScoped
from stapel_core.django.mandate import MANDATE_FUNCTION, MANDATE_RESULT_KEY


@pytest.fixture
def mandate_seam():
    """Wire the seam and hand back the switch for its answer.

    A deployment that CAN ask is the whole premise: with nothing wired there
    are no mandates to hold and the third state does not exist.
    """
    state = {"has_mandate": False, "raises": None}

    def handler(payload):
        if state["raises"]:
            raise state["raises"]
        return {MANDATE_RESULT_KEY: state["has_mandate"]}

    function_registry.register(MANDATE_FUNCTION, handler)
    yield state
    function_registry._providers.pop(MANDATE_FUNCTION, None)


@pytest.fixture(autouse=True)
def _clear_mandate_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def someone_elses_event(user):
    from stapel_calendar import services

    return services.create_event(
        owner=user,
        title="Board meeting",
        start="2026-03-02T09:00:00+00:00",
        end="2026-03-02T10:00:00+00:00",
    )


@pytest.fixture
def outsider_client(api_client, other_user):
    """A real, registered account. Not anonymous — and holding nothing."""
    api_client.force_authenticate(user=other_user)
    return api_client


# ---------------------------------------------------------------------------
# The hole
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mandate_less_account_cannot_read_an_event_by_id(
    mandate_seam, outsider_client, someone_elses_event
):
    resp = outsider_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}")
    assert resp.status_code == 403, resp.content


@pytest.mark.django_db
def test_a_mandate_less_account_cannot_download_the_ics(
    mandate_seam, outsider_client, someone_elses_event
):
    resp = outsider_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}/ics")
    assert resp.status_code == 403, resp.content


@pytest.mark.django_db
def test_a_mandate_less_account_cannot_mutate_by_id(
    mandate_seam, outsider_client, someone_elses_event
):
    """Mutations were owner-only already; the gate refuses before that check,
    so a probe cannot even learn the event exists."""
    resp = outsider_client.patch(
        f"/calendar/api/v1/events/{someone_elses_event.id}",
        {"title": "hijacked"},
        format="json",
    )
    assert resp.status_code == 403, resp.content


# ---------------------------------------------------------------------------
# The three states stay three
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mandated_account_still_reads_the_event(
    mandate_seam, outsider_client, someone_elses_event
):
    mandate_seam["has_mandate"] = True
    resp = outsider_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}")
    assert resp.status_code == 200, resp.content


@pytest.mark.django_db
def test_could_not_ask_refuses_with_503_never_403(
    mandate_seam, outsider_client, someone_elses_event
):
    """The distinction the whole axis exists for: "you hold no mandate" is a
    verdict, "workspaces is unreachable" is not, and a 403 for the second is
    how a routing skew tells a workspace owner they are Forbidden."""
    mandate_seam["raises"] = RuntimeError("workspaces is down")
    resp = outsider_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}")
    assert resp.status_code == 503, resp.content


@pytest.mark.django_db
def test_an_anonymous_session_is_still_refused(mandate_seam, api_client, someone_elses_event):
    from stapel_core.django.users.models import User

    api_client.force_authenticate(user=User.create_anonymous_user())
    resp = api_client.get(f"/calendar/api/v1/events/{someone_elses_event.id}")
    assert resp.status_code in (401, 403), resp.content


def test_the_gate_is_the_mandate_one_not_the_two_state_one():
    """Pins the class itself: ``IsNotAnonymousUser`` here IS the hole."""
    for view in (views.EventDetailView, views.EventICSView):
        assert HasWorkspaceMandateIfScoped in view.permission_classes, view.__name__
