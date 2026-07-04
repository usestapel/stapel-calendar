from django.apps import AppConfig


class CalendarConfig(AppConfig):
    name = "stapel_calendar"
    label = "calendar"
    verbose_name = "Calendar and scheduling"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm functions/actions, system checks,
        # error-key registration. Keep each in its own module.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401
