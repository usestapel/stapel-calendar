"""Action subscriptions of stapel-calendar.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery). Consumes contracts live in ``schemas/consumes/``.

The GDPR erasure protocol does not live here. Since 0.6.0 the module
registers as a stapel-gdpr data owner from ``apps.ready()`` and
:func:`stapel_core.gdpr.register_gdpr_owner` subscribes all three actions —
``gdpr.erasure.requested`` (erase + receipt), ``gdpr.owner.probe``
(``gdpr.owner.alive`` answered from the SAME subscriber, which is what makes
the answer evidence that the erasure path is consumed) and the deprecated
``user.deleted``. All three run
:func:`stapel_calendar.erasure.erase_subject`; a second handler for
``user.deleted`` here would be a second erasure to keep in step.

What DOES live here is the other half of an account's life cycle:

- ``user.merged`` (from stapel-auth) — an anonymous guest was absorbed into
  an existing account; carry the guest's calendar onto the survivor. It is
  written here rather than beside the erasure because it is not an erasure:
  nothing is deleted, rows change owner.
"""
import logging

from django.core.exceptions import ValidationError

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has calendar rows to carry over but there
    is no local user row to point their FKs at yet. Raising is the comm
    layer's retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away guest's calendar onto the surviving account.

    stapel-auth absorbs an anonymous guest into an existing account and then
    DELETES the guest row. Every per-user column in this module is a
    ``CASCADE`` FK to ``AUTH_USER_MODEL``, so without this handler the
    guest's calendar goes with them — a visitor who booked a slot before
    signing in signs in and the booking is gone, with no erasure ever
    requested for it. Three models are re-pointed here, in one transaction,
    before that deletion cascades:

    * :class:`~stapel_calendar.models.Event` — ``owner``. Occurrences are
      ``Event`` rows too (``recurrence_parent`` set) and carry their own
      ``owner``, so filtering by owner id moves masters and materialized
      occurrences alike; ``cal_event_uniq_occurrence`` is scoped to
      ``(recurrence_parent, recurrence_id)`` and cannot be violated by a
      change of owner.
    * :class:`~stapel_calendar.models.Participant` — ``user``. Deduplicated
      against ``cal_participant_uniq`` first: both accounts may be invited
      to the same event, and there the survivor's own row wins — with its
      RSVP, which they answered as themselves — and the guest's duplicate is
      dropped rather than reassigned into a constraint violation.
    * :class:`~stapel_calendar.models.AvailabilityWindow` — ``user``. No
      user-scoped constraint, so a plain rewrite. Overlapping windows are
      legal here (free/busy unions them), so two accounts' windows simply
      add up.

    Other people's events are untouched: only the guest's own participation
    rows move, which is the same line ``erase_subject`` draws.

    Two different "unknown id" situations, and conflating them loses data:

    * the guest owns nothing here (never opened a calendar, or a previous
      delivery already moved it all) — a genuine no-op, returned quietly;
      this is also the at-least-once idempotency path;
    * the guest owns rows but the survivor has no user row here yet — NOT a
      no-op. :class:`MergeTargetNotReady` is raised so the event is
      redelivered, because returning success would let the outbox mark it
      delivered and lose the calendar for good.

    A malformed id is neither: it names no row here and no redelivery can fix
    it, so it is logged and dropped rather than raised into a poison loop.
    Django's ``UUIDField`` raises ``ValidationError``, which is not a
    ``ValueError`` — both are caught.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .models import AvailabilityWindow, Event, Participant

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    with transaction.atomic():
        # Every read, and the decision they feed, happens inside the
        # transaction and before the first write, so the "not yet" path below
        # can never leave half the rows moved.
        try:
            guest_participations = list(
                Participant.objects.filter(user_id=from_user_id)
            )
            owns_events = Event.objects.filter(owner_id=from_user_id).exists()
            owns_windows = AvailabilityWindow.objects.filter(
                user_id=from_user_id
            ).exists()
            # The survivor probe is read under the same guard, because a
            # malformed *into* id must not escape as a poison pill either.
            survivor_exists = (
                get_user_model().objects.filter(pk=into_user_id).exists()
            )
            survivor_event_ids = set(
                Participant.objects.filter(user_id=into_user_id).values_list(
                    "event_id", flat=True
                )
            )
        except (ValidationError, ValueError, TypeError):
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not (guest_participations or owns_events or owns_windows):
            return
        if not survivor_exists:
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-calendar yet; redeliver "
                f"once its projection has landed"
            )

        moved_participations = 0
        dropped_participations = 0
        for participation in guest_participations:
            if participation.event_id in survivor_event_ids:
                # The survivor is already on this event: their RSVP stays.
                participation.delete()
                dropped_participations += 1
                continue
            participation.user_id = into_user_id
            participation.save(update_fields=["user", "updated_at"])
            survivor_event_ids.add(participation.event_id)
            moved_participations += 1

        moved_events = Event.objects.filter(owner_id=from_user_id).update(
            owner_id=into_user_id
        )
        moved_windows = AvailabilityWindow.objects.filter(
            user_id=from_user_id
        ).update(user_id=into_user_id)

    logger.info(
        "user.merged %s -> %s: %s events, %s participations moved, %s dropped "
        "as duplicates, %s availability windows carried over",
        from_user_id,
        into_user_id,
        moved_events,
        moved_participations,
        dropped_participations,
        moved_windows,
    )


__all__ = ["MergeTargetNotReady", "handle_user_merged"]
