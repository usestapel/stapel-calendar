"""Django system checks for stapel-calendar configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that only degrade lazily.

- SCOPE_PROVIDER unimportable / not a ScopeProvider -> E (create & list
  cannot resolve/filter scope).
- REMINDER_POLICY unimportable -> W (only reminders degrade; the calendar
  still serves).
- REMINDER_OFFSETS not a list of non-negative ints -> E (would crash the
  reminder cron with a confusing error).
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_scope_provider(app_configs, **kwargs):
    from .conf import calendar_settings
    from .scope import ScopeProvider

    try:
        provider = calendar_settings.SCOPE_PROVIDER
    except Exception as exc:
        return [
            checks.Error(
                f"STAPEL_CALENDAR['SCOPE_PROVIDER'] could not be imported: {exc}",
                id="stapel_calendar.E001",
            )
        ]
    target = provider if isinstance(provider, type) else type(provider)
    if not issubclass(target, ScopeProvider):
        return [
            checks.Error(
                "STAPEL_CALENDAR['SCOPE_PROVIDER'] must be a ScopeProvider subclass",
                id="stapel_calendar.E002",
            )
        ]
    return []


@checks.register(checks.Tags.compatibility)
def check_reminder_policy(app_configs, **kwargs):
    from .conf import calendar_settings

    try:
        calendar_settings.REMINDER_POLICY
    except Exception as exc:
        return [
            checks.Warning(
                f"STAPEL_CALENDAR['REMINDER_POLICY'] could not be imported: {exc}. "
                "Reminders are disabled until this resolves.",
                id="stapel_calendar.W001",
            )
        ]
    return []


@checks.register(checks.Tags.compatibility)
def check_reminder_offsets(app_configs, **kwargs):
    from .conf import calendar_settings

    offsets = calendar_settings.REMINDER_OFFSETS
    if not isinstance(offsets, (list, tuple)) or any(
        not isinstance(o, int) or o < 0 for o in offsets
    ):
        return [
            checks.Error(
                "STAPEL_CALENDAR['REMINDER_OFFSETS'] must be a list of "
                "non-negative integers (minutes).",
                id="stapel_calendar.E003",
            )
        ]
    return []
