"""RSVP flow, event-driven reminders, and the reminder-policy seam."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from stapel_calendar import services
from stapel_calendar.models import Participant, RSVP
from stapel_calendar.reminders import (
    DefaultReminderPolicy,
    DueReminder,
    ReminderPolicy,
    run_reminders,
)

UTC = ZoneInfo("UTC")


@pytest.mark.django_db
class TestRSVP:
    def test_owner_is_accepted_participant(self, user):
        ev = services.create_event(
            owner=user, title="M",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        )
        p = Participant.objects.get(event=ev, user=user)
        assert p.rsvp == RSVP.ACCEPTED

    def test_respond_updates_rsvp(self, user, other_user):
        ev = services.create_event(
            owner=user, title="M",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
            participant_ids=[str(other_user.id)],
        )
        services.respond(ev, other_user, RSVP.ACCEPTED)
        assert Participant.objects.get(event=ev, user=other_user).rsvp == RSVP.ACCEPTED

    def test_respond_uninvited_raises(self, user, other_user):
        ev = services.create_event(
            owner=user, title="M",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        )
        with pytest.raises(Participant.DoesNotExist):
            services.respond(ev, other_user, RSVP.ACCEPTED)


@pytest.mark.django_db
class TestReminders:
    def test_reminder_due_emitted_in_window(self, user, captured_events, settings):
        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [10]}
        now = timezone.now()
        # Event starts in 10 minutes -> the 10-min reminder fires "now".
        services.create_event(
            owner=user, title="Soon",
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=40),
        )
        emitted = run_reminders(now)
        assert emitted == 1
        names = [e.event_type for e in captured_events]
        assert names.count("calendar.event.reminder_due") == 1

    def test_reminder_not_due_yet(self, user, captured_events, settings):
        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [10]}
        now = timezone.now()
        # Starts in an hour; the 10-min reminder is not due for ~50 min.
        services.create_event(
            owner=user, title="Later",
            start=now + timedelta(hours=1),
            end=now + timedelta(hours=2),
        )
        assert run_reminders(now) == 0

    def test_reminder_payload_has_dedup_key(self, user, captured_events, settings):
        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [10]}
        now = timezone.now()
        ev = services.create_event(
            owner=user, title="Soon",
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=40),
        )
        run_reminders(now)
        evt = next(
            e for e in captured_events if e.event_type == "calendar.event.reminder_due"
        )
        assert evt.payload["dedup_key"] == f"{ev.id}:10"

    def test_cancelled_event_no_reminder(self, user, captured_events, settings):
        from stapel_calendar.models import Event, EventStatus

        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [10]}
        now = timezone.now()
        ev = services.create_event(
            owner=user, title="Soon",
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=40),
        )
        Event.objects.filter(id=ev.id).update(status=EventStatus.CANCELLED)
        assert run_reminders(now) == 0


@pytest.mark.django_db
class TestReminderPolicySeam:
    """Extension seam #2 — swap the reminder policy without forking."""

    def test_custom_policy_is_used(self, user, captured_events, settings):
        calls = {"n": 0}

        class SilentPolicy(ReminderPolicy):
            def due_for(self, event, now, window):
                return [DueReminder(event.id, 0, now)]

            def emit_for(self, event, reminder):
                calls["n"] += 1  # deliberately does NOT emit

        # Register the class on a module so import_string can find it.
        import stapel_calendar.reminders as rem
        rem._TestSilentPolicy = SilentPolicy
        settings.STAPEL_CALENDAR = {
            "REMINDER_POLICY": "stapel_calendar.reminders._TestSilentPolicy",
        }
        now = timezone.now()
        services.create_event(
            owner=user, title="Soon",
            start=now + timedelta(minutes=5),
            end=now + timedelta(minutes=35),
        )
        run_reminders(now)
        assert calls["n"] == 1
        # Custom policy suppressed the emit.
        assert not any(
            e.event_type == "calendar.event.reminder_due" for e in captured_events
        )
        del rem._TestSilentPolicy

    def test_default_policy_offsets_read_from_settings(self, settings):
        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [5, 60]}
        assert DefaultReminderPolicy().offsets == [5, 60]
