"""Action subscriptions of stapel-calendar.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery). Consumes contracts live in ``schemas/consumes/``.
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed:
    the user's owned events (and their occurrences/participants), their
    participations in other events, and their availability windows."""
    from .gdpr import CalendarGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    CalendarGDPRProvider().delete(user_id)
    logger.info("calendar data erased for deleted user %s", user_id)
