"""stapel-calendar answers the erasure protocol — probe, erase, receipt, once.

The finding this closes was found on a live stand: stapel-calendar was a
declared data owner with no ``gdpr.owner.probe`` subscriber. Its in-process
provider really did erase and its old ``user.deleted`` handler really did
run, and the fleet's erasure still waited on this owner forever, because
liveness is answered by the subscriber that erases and there was none to
answer.

``VALIDATE_SCHEMAS`` is on (``_codegen_settings``), so every receipt these
tests capture was validated against ``schemas/emits/gdpr.section.erased.json``
on the way out, and every payload fed to the handlers went through
stapel-core's consumes contract.
"""
import uuid
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from zoneinfo import ZoneInfo

from stapel_core.comm import action_registry
from stapel_core.gdpr import register_gdpr_owner, registered_gdpr_owners

from stapel_calendar import services
from stapel_calendar.erasure import OWNER, SUBJECT_TYPES, erase_subject
from stapel_calendar.models import AvailabilityWindow, Event, Participant

UTC = ZoneInfo("UTC")

#: The registration ``apps.ready()`` made. Same terms means the helper hands
#: back the existing registration rather than subscribing twice, so this is
#: both how the tests reach the handlers and an assertion that ready() ran.
CALENDAR_OWNER = register_gdpr_owner(OWNER, SUBJECT_TYPES, erase_subject)


def _event(**payload):
    return SimpleNamespace(payload=payload, event_id="evt-1", service="gdpr")


def _trail(user, other_user):
    """One row in every table calendar erases, so a count can be wrong out loud.

    Two owned events (a master plus one materialized occurrence), one
    participation in somebody else's event, one availability window.
    """
    series = services.create_event(
        owner=user,
        title="Weekly",
        start=datetime(2026, 1, 5, 9, tzinfo=UTC),
        end=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
        recurrence_type="weekly",
    )
    services.materialize(series, datetime(2026, 1, 12, 9, tzinfo=UTC))
    theirs = services.create_event(
        owner=other_user,
        title="Theirs",
        start=datetime(2026, 1, 6, 9, tzinfo=UTC),
        end=datetime(2026, 1, 6, 10, tzinfo=UTC),
        participant_ids=[str(user.id)],
    )
    AvailabilityWindow.objects.create(
        user=user, weekday=0, start_time=time(9), end_time=time(17), timezone="UTC"
    )
    return series, theirs


@pytest.mark.django_db
class TestRegistration:
    """What ``apps.ready()`` put on the bus."""

    def test_the_erasure_and_probe_handlers_are_subscribed(self):
        assert action_registry.handlers("gdpr.erasure.requested")
        assert action_registry.handlers("gdpr.owner.probe")
        # The deprecated account signal, until stapel-gdpr 0.6.0 drops it.
        assert action_registry.handlers("user.deleted")

    def test_the_owner_claims_the_account_and_nothing_else(self):
        from stapel_calendar.gdpr import CalendarGDPRProvider

        assert registered_gdpr_owners()["calendar"] == ("account",)
        # One name, or the receipts land on nobody's part.
        assert OWNER == "calendar" == CalendarGDPRProvider.section


@pytest.mark.django_db
class TestProbe:
    """`calendar: alive=false` — the symptom this release exists to end."""

    def test_the_probe_is_answered_with_what_this_module_erases(self):
        correlation_id = str(uuid.uuid4())
        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_owner_probe(_event(correlation_id=correlation_id))

        name, payload = m_emit.call_args.args
        assert name == "gdpr.owner.alive"
        assert payload == {
            "owner": "calendar",
            "subject_types": ["account"],
            "correlation_id": correlation_id,
        }

    def test_the_probe_is_answered_even_without_a_correlation_id(self):
        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_owner_probe(_event())

        name, payload = m_emit.call_args.args
        assert name == "gdpr.owner.alive"
        assert payload == {"owner": "calendar", "subject_types": ["account"]}


@pytest.mark.django_db
class TestErasure:
    """The receipt says what was erased, and only what was erased."""

    def test_the_trail_goes_and_the_receipt_counts_it(self, user, other_user):
        series, theirs = _trail(user, other_user)
        correlation_id = str(uuid.uuid4())

        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_erasure_requested(_event(
                request_id=1,
                correlation_id=correlation_id,
                subject_type="account",
                subject_key=str(user.id),
            ))

        name, payload = m_emit.call_args.args
        assert name == "gdpr.section.erased"
        assert payload["owner"] == "calendar"
        assert payload["subject_type"] == "account"
        assert payload["subject_key"] == str(user.id)
        assert payload["correlation_id"] == correlation_id
        assert payload["receipt_id"] == f"calendar:account:{user.id}:{correlation_id}"
        # Two owned events (master + materialized occurrence); the RSVP rows
        # ON those events cascaded away and are NOT counted here — only the
        # participation left over in somebody else's event is this module's
        # Participant row to claim.
        assert payload["counts"] == {
            "events": 2,
            "participations": 1,
            "availability_windows": 1,
        }

        assert not Event.objects.filter(owner_id=user.id).exists()
        assert not Participant.objects.filter(user_id=user.id).exists()
        assert not AvailabilityWindow.objects.filter(user_id=user.id).exists()
        # The other person's event is their record and survives, minus the
        # erased subject's attendance.
        assert Event.objects.filter(id=theirs.id).exists()
        assert Participant.objects.filter(event=theirs, user=other_user).exists()

    def test_redelivery_erases_nothing_twice_and_mints_the_same_receipt(
        self, user, other_user
    ):
        _trail(user, other_user)
        event = _event(
            request_id=2,
            correlation_id="corr-redelivered",
            subject_type="account",
            subject_key=str(user.id),
        )

        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_erasure_requested(event)
            CALENDAR_OWNER.handle_erasure_requested(event)

        first, second = [call.args[1] for call in m_emit.call_args_list]
        assert first["counts"]["events"] == 2
        assert set(second["counts"].values()) == {0}
        assert first["receipt_id"] == second["receipt_id"]

    def test_an_unclaimed_subject_is_ignored_without_a_receipt(self, user, other_user):
        """gdpr creates a part only for owners that claim the type, so a
        receipt here would be answering for somebody else."""
        _trail(user, other_user)

        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_erasure_requested(_event(
                request_id=3,
                correlation_id="corr-workspace",
                subject_type="workspace",
                subject_key="ws-1",
            ))

        m_emit.assert_not_called()
        assert Event.objects.filter(owner_id=user.id).exists()
        assert erase_subject("workspace", "ws-1") is None

    def test_a_malformed_request_is_dropped_without_a_receipt(self, user, other_user):
        """A payload this shape will never parse; raising would redeliver it."""
        _trail(user, other_user)

        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_erasure_requested(_event(
                correlation_id="corr-broken", subject_type="account",
            ))

        m_emit.assert_not_called()
        assert Event.objects.filter(owner_id=user.id).exists()

    def test_the_deprecated_signal_runs_the_same_erasure(self, user, other_user):
        """user.deleted and gdpr.erasure.requested reach one implementation,
        so deleting the legacy handler deletes no erasure logic."""
        _trail(user, other_user)

        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_user_deleted(_event(
                user_id=str(user.id), correlation_id="corr-legacy",
            ))

        name, payload = m_emit.call_args.args
        assert name == "gdpr.section.erased"
        assert payload["counts"]["events"] == 2
        assert payload["user_id"] == str(user.id)
        assert not Event.objects.filter(owner_id=user.id).exists()


@pytest.mark.django_db
class TestBothPathsInOneProcess:
    """A monolith runs the in-process provider AND this subscriber.

    stapel-calendar is not the module that hosts stapel-gdpr, so the two
    paths cannot be driven end to end from this repo's test instance. What
    can be pinned here is the property the host depends on: one erasure, one
    receipt, and it is the subscriber's — the provider is a silent caller of
    the same function, so whichever path runs second finds nothing left and
    cannot overwrite the honest counts with its zeroes.
    """

    def test_the_in_process_provider_erases_and_receipts_nothing(
        self, user, other_user
    ):
        from stapel_calendar.gdpr import CalendarGDPRProvider

        _trail(user, other_user)

        with patch("stapel_core.comm.emit") as m_emit:
            CalendarGDPRProvider().delete(user.id)

        m_emit.assert_not_called()
        assert not Event.objects.filter(owner_id=user.id).exists()

    def test_subscriber_then_provider_leaves_exactly_one_receipt(
        self, user, other_user
    ):
        from stapel_calendar.gdpr import CalendarGDPRProvider

        _trail(user, other_user)
        correlation_id = str(uuid.uuid4())

        with patch("stapel_core.comm.emit") as m_emit:
            CALENDAR_OWNER.handle_erasure_requested(_event(
                request_id=4,
                correlation_id=correlation_id,
                subject_type="account",
                subject_key=str(user.id),
            ))
            CalendarGDPRProvider().delete(user.id)
            CALENDAR_OWNER.handle_user_deleted(_event(user_id=str(user.id)))

        receipts = [
            call.args[1] for call in m_emit.call_args_list
            if call.args[0] == "gdpr.section.erased"
        ]
        assert len(receipts) == 1
        assert receipts[0]["counts"]["events"] == 2
        assert receipts[0]["correlation_id"] == correlation_id
