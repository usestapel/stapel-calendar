"""Action subscriptions of stapel-calendar.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery). Consumes contracts live in ``schemas/consumes/``.

This module currently subscribes nothing. The GDPR erasure protocol used to
live here as a hand-rolled ``user.deleted`` handler; since 0.6.0 the module
registers as a stapel-gdpr data owner from ``apps.ready()`` and
:func:`stapel_core.gdpr.register_gdpr_owner` subscribes all three actions —
``gdpr.erasure.requested`` (erase + receipt), ``gdpr.owner.probe``
(``gdpr.owner.alive`` answered from the SAME subscriber, which is what makes
the answer evidence that the erasure path is consumed) and the deprecated
``user.deleted``. All three run
:func:`stapel_calendar.erasure.erase_subject`; a second handler for
``user.deleted`` here would be a second erasure to keep in step.

The module stays because it is where this library's consumes belong, and the
next one should land beside that history rather than in a new file.
"""
