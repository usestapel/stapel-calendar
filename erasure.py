"""What stapel-calendar erases when a subject is erased — one operation.

stapel-calendar was a declared data owner that answered no
``gdpr.owner.probe``: its :class:`~stapel_calendar.gdpr.CalendarGDPRProvider`
ran in-process in a monolith and the ``user.deleted`` handler in
``actions.py`` really did erase, but an owners-health board reported
``calendar: alive=false`` forever, because liveness is answered by the
subscriber that erases and there was none. A fleet's erasure then waits on
this module until it times out.

So the operation lives here once and is reached three ways:

* :class:`~stapel_calendar.gdpr.CalendarGDPRProvider` — the in-process
  registry the orchestrator walks in a monolith (``provider.delete(user_id)``);
* the ``gdpr.erasure.requested`` subscriber that
  :func:`stapel_core.gdpr.register_gdpr_owner` builds from this callable in
  ``apps.ready()`` — the path that also answers the probe;
* the deprecated ``user.deleted`` signal, subscribed by the same helper
  (stapel-gdpr still emits it for one minor). ``actions.py`` no longer
  carries its own handler: two handlers for one signal is two erasures to
  keep in step.

``erase_subject`` deletes; it does not anonymize. A calendar row is the
person's own trail — the events they called, the invitations they accepted,
the hours they said they were free — and an event stripped of who owns it is
not a record anybody keeps. Other people's events survive: only the erased
subject's participation row is removed from them.
"""
from __future__ import annotations

#: The name this module answers to in ``STAPEL_GDPR["DATA_OWNERS"]`` — the
#: same string as ``CalendarGDPRProvider.section``, because an owner with two
#: names is an owner whose receipts land on nobody's part.
OWNER = "calendar"

#: Subject types this module can really erase, and therefore the only ones it
#: claims and answers ``gdpr.owner.alive`` with. Every row here hangs off one
#: user id; ``scope_key`` is an opaque string a host's provider computes, not
#: a workspace id this module could match a workspace erasure on, so claiming
#: ``workspace`` would mint a receipt for work nobody could have done.
SUBJECT_TYPES = ("account",)


def erase_subject(subject_type: str, subject_key, workspace_id=None) -> dict | None:
    """Erase one subject's calendar trail. Returns the receipt's counts.

    ``None`` means "this key names nothing of mine" — the subject type is not
    one this module claims, so the caller owes no receipt (stapel-gdpr creates
    a part only for owners that claim the type).

    Idempotent: every row is matched by user id, so a redelivery matches
    nothing and receipts its zeroes rather than pretending the work happened
    twice. ``workspace_id`` is accepted and ignored — an account request may
    carry it as a partition hint for owners that need one, and narrowing by it
    here would leave the subject's calendar in every other tenant.

    A key this module cannot parse raises ``ValueError`` / ``ValidationError``
    out of the ORM, which the protocol handler logs and never receipts: an
    unusable key names no row here, and a receipt would claim an erasure that
    did not happen.
    """
    if subject_type not in SUBJECT_TYPES:
        return None
    # Never stringified: the user pk is a UUID in some deployments and an
    # integer in others, and both spellings must reach the ORM as they came —
    # the protocol hands over a string, the in-process provider hands over
    # whatever the host's pk is, and a str() around the latter turns a valid
    # integer key into an unparseable one.
    key = subject_key.strip() if isinstance(subject_key, str) else subject_key
    if key is None or key == "":
        return None

    from .models import AvailabilityWindow, Event, Participant

    # Owned events first: the delete cascades to their occurrences (an
    # occurrence is an Event with a recurrence_parent) and to every
    # participant row on them, including other people's RSVPs — those rows are
    # attendance at a meeting that no longer exists.
    events = _deleted(Event, owner_id=key)
    # What is left: this subject's attendance in OTHER people's events. Those
    # events stay — they are their owners' record — minus this RSVP.
    participations = _deleted(Participant, user_id=key)
    windows = _deleted(AvailabilityWindow, user_id=key)
    return {
        "events": events,
        "participations": participations,
        "availability_windows": windows,
    }


def _deleted(model, **lookup) -> int:
    """Rows of *model* this run actually removed — what a receipt may claim.

    Django's ``delete()`` returns the total across cascades; the per-label
    breakdown beside it is the honest number for this model, so a count never
    inflates itself with somebody else's rows.
    """
    _, per_model = model.objects.filter(**lookup).delete()
    return per_model.get(model._meta.label, 0)


__all__ = ["OWNER", "SUBJECT_TYPES", "erase_subject"]
