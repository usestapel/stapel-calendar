from django.apps import AppConfig


class CalendarConfig(AppConfig):
    name = "stapel_calendar"
    label = "calendar"
    verbose_name = "Calendar and scheduling"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm functions/actions, system checks,
        # error-key registration. Keep each in its own module.
        from . import actions  # noqa: F401
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # GDPR: register the per-app data handler (monolith in-process mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import CalendarGDPRProvider

        if not any(p.section == "calendar" for p in gdpr_registry.providers):
            gdpr_registry.register(CalendarGDPRProvider())

        # The erasure protocol (stapel-gdpr 0.5.0+), implemented once in
        # stapel-core: gdpr.erasure.requested -> erase -> gdpr.section.erased
        # with a deterministic receipt inside the erase's transaction, plus
        # the gdpr.owner.probe answer from the same module, plus the
        # deprecated user.deleted. No protocol code is written here.
        #
        # Unconditional, unlike the in-process provider above: until 0.6.0
        # this module was a declared data owner that answered no probe, so a
        # fleet's owners-health said `calendar: alive=false` and every
        # erasure waited on it forever while the monolith path erased fine.
        # Liveness is answered by the subscriber that erases or it is not
        # evidence of anything.
        from stapel_core.gdpr import register_gdpr_owner

        from .erasure import OWNER, SUBJECT_TYPES, erase_subject

        register_gdpr_owner(OWNER, SUBJECT_TYPES, erase_subject)
