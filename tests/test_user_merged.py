"""A merge is not a delete — the other half of an account's life cycle.

stapel-auth folds an anonymous guest into an existing account on sign-in and
then deletes the guest row. Every per-user column here is a ``CASCADE`` FK,
so before ``user.merged`` was consumed the guest's calendar went with them
silently: nothing raised, nothing retried, nothing was logged, and the first
report was a person saying the slot they booked before signing in was gone.

These tests pin the three models the handler re-points, the duplicate rule
that keeps ``cal_participant_uniq`` satisfiable, and the four ways a payload
can be wrong without becoming a poison pill.
"""
from datetime import datetime, time
from types import SimpleNamespace

import pytest
from zoneinfo import ZoneInfo

from stapel_core.comm import action_registry

from stapel_calendar import services
from stapel_calendar.actions import MergeTargetNotReady, handle_user_merged
from stapel_calendar.models import AvailabilityWindow, Event, Participant

UTC = ZoneInfo("UTC")


def _event(**payload):
    return SimpleNamespace(payload=payload, event_id="evt-merge-1", service="auth")


def _guest_trail(guest, host):
    """One row in every table the merge carries over.

    An owned series plus one materialized occurrence, an RSVP in somebody
    else's event, an availability window.
    """
    series = services.create_event(
        owner=guest,
        title="Guest weekly",
        start=datetime(2026, 3, 2, 9, tzinfo=UTC),
        end=datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
        recurrence_type="weekly",
    )
    services.materialize(series, datetime(2026, 3, 9, 9, tzinfo=UTC))
    theirs = services.create_event(
        owner=host,
        title="Hosted",
        start=datetime(2026, 3, 3, 9, tzinfo=UTC),
        end=datetime(2026, 3, 3, 10, tzinfo=UTC),
        participant_ids=[str(guest.id)],
    )
    AvailabilityWindow.objects.create(
        user=guest, weekday=1, start_time=time(9), end_time=time(17), timezone="UTC"
    )
    return series, theirs


@pytest.fixture
def survivor(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username="survivor", email="survivor@example.com", password="x"
    )


@pytest.mark.django_db
class TestSubscription:
    def test_user_merged_is_subscribed(self):
        """The pair the lifecycle check reads: this module answers both."""
        assert action_registry.handlers("user.deleted")
        assert handle_user_merged in action_registry.handlers("user.merged")

    def test_the_lifecycle_pair_check_is_green(self):
        """``stapel_core.lifecycle.E001`` with this app loaded and ready().

        The ``user.deleted`` half is a closure stapel-core subscribes on this
        library's behalf from ``register_gdpr_owner``; core stamps it with
        this module's name, so the pair is charged here and not to core. If
        that stamp ever stops working this assertion turns red in THIS repo,
        which is where somebody can act on it.
        """
        from stapel_core.comm.lifecycle_checks import check_lifecycle_pairs

        assert check_lifecycle_pairs() == []


@pytest.mark.django_db
class TestReparenting:
    def test_the_guest_calendar_lands_on_the_survivor(self, user, other_user, survivor):
        series, theirs = _guest_trail(user, other_user)

        handle_user_merged(
            _event(from_user_id=str(user.id), into_user_id=str(survivor.id))
        )

        # Master + materialized occurrence both change owner.
        assert Event.objects.filter(owner_id=survivor.id).count() == 2
        assert not Event.objects.filter(owner_id=user.id).exists()
        # The RSVP in somebody else's event moves; the event itself does not.
        assert Participant.objects.filter(event=theirs, user=survivor).exists()
        assert not Participant.objects.filter(user_id=user.id).exists()
        assert Event.objects.get(id=theirs.id).owner_id == other_user.id
        assert AvailabilityWindow.objects.filter(user_id=survivor.id).count() == 1
        assert not AvailabilityWindow.objects.filter(user_id=user.id).exists()

    def test_a_duplicate_rsvp_keeps_the_survivors_own_answer(
        self, user, other_user, survivor
    ):
        """Both accounts on one event: ``cal_participant_uniq`` allows one row.

        The survivor answered as themselves; that answer is the one that
        stays, and the guest's duplicate is dropped rather than reassigned
        into a constraint violation.
        """
        hosted = services.create_event(
            owner=other_user,
            title="Both invited",
            start=datetime(2026, 3, 4, 9, tzinfo=UTC),
            end=datetime(2026, 3, 4, 10, tzinfo=UTC),
            participant_ids=[str(user.id), str(survivor.id)],
        )
        Participant.objects.filter(event=hosted, user=survivor).update(rsvp="accepted")
        Participant.objects.filter(event=hosted, user=user).update(rsvp="declined")

        handle_user_merged(
            _event(from_user_id=str(user.id), into_user_id=str(survivor.id))
        )

        rows = Participant.objects.filter(event=hosted, user=survivor)
        assert rows.count() == 1
        assert rows.get().rsvp == "accepted"
        assert not Participant.objects.filter(event=hosted, user_id=user.id).exists()

    def test_redelivery_changes_nothing_further(self, user, other_user, survivor):
        """Delivery is at-least-once; the second run is the idempotency path."""
        _guest_trail(user, other_user)
        payload = _event(from_user_id=str(user.id), into_user_id=str(survivor.id))

        handle_user_merged(payload)
        after_first = {
            "events": list(
                Event.objects.filter(owner_id=survivor.id)
                .order_by("id")
                .values_list("id", flat=True)
            ),
            "participants": list(
                Participant.objects.filter(user_id=survivor.id)
                .order_by("id")
                .values_list("id", flat=True)
            ),
            "windows": list(
                AvailabilityWindow.objects.filter(user_id=survivor.id)
                .order_by("id")
                .values_list("id", flat=True)
            ),
        }

        handle_user_merged(payload)

        assert after_first["events"] == list(
            Event.objects.filter(owner_id=survivor.id)
            .order_by("id")
            .values_list("id", flat=True)
        )
        assert after_first["participants"] == list(
            Participant.objects.filter(user_id=survivor.id)
            .order_by("id")
            .values_list("id", flat=True)
        )
        assert after_first["windows"] == list(
            AvailabilityWindow.objects.filter(user_id=survivor.id)
            .order_by("id")
            .values_list("id", flat=True)
        )

    def test_a_guest_this_module_never_saw_is_a_quiet_no_op(
        self, user, other_user, survivor
    ):
        """Most merges name a guest who never opened a calendar."""
        import uuid

        _guest_trail(user, other_user)
        before = Event.objects.count()

        handle_user_merged(
            _event(from_user_id=str(uuid.uuid4()), into_user_id=str(survivor.id))
        )

        assert Event.objects.count() == before
        assert not Event.objects.filter(owner_id=survivor.id).exists()

    def test_merging_an_account_into_itself_does_nothing(self, user, other_user):
        _guest_trail(user, other_user)

        handle_user_merged(
            _event(from_user_id=str(user.id), into_user_id=str(user.id))
        )

        assert Event.objects.filter(owner_id=user.id).count() == 2


@pytest.mark.django_db
class TestOrderingAndPoison:
    def test_rows_to_move_and_no_survivor_yet_asks_for_a_redelivery(
        self, user, other_user
    ):
        """Not a no-op: returning success would lose the calendar for good."""
        import uuid

        _guest_trail(user, other_user)
        absent = str(uuid.uuid4())

        with pytest.raises(MergeTargetNotReady):
            handle_user_merged(
                _event(from_user_id=str(user.id), into_user_id=absent)
            )

        # Nothing half-moved: the whole decision is read before the first write.
        assert Event.objects.filter(owner_id=user.id).count() == 2
        assert AvailabilityWindow.objects.filter(user_id=user.id).count() == 1

    def test_a_malformed_id_is_logged_and_dropped(self, user, other_user, survivor):
        """``UUIDField`` raises ``ValidationError``, which is not a ``ValueError``.

        An escaping exception is a poison pill: no redelivery can fix a typo,
        so the bus would replay it until it gives up.
        """
        _guest_trail(user, other_user)

        handle_user_merged(
            _event(from_user_id="not-a-uuid", into_user_id=str(survivor.id))
        )
        handle_user_merged(
            _event(from_user_id=str(user.id), into_user_id="not-a-uuid")
        )

        assert Event.objects.filter(owner_id=user.id).count() == 2

    def test_a_payload_missing_an_id_does_not_raise(self, user, other_user, survivor):
        _guest_trail(user, other_user)

        handle_user_merged(_event(into_user_id=str(survivor.id)))
        handle_user_merged(_event(from_user_id=str(user.id)))
        handle_user_merged(_event())

        assert Event.objects.filter(owner_id=user.id).count() == 2
