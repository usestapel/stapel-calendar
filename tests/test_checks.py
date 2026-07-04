"""System checks fire on misconfiguration (extension-point safety net)."""
from stapel_calendar.checks import (
    check_reminder_offsets,
    check_reminder_policy,
    check_scope_provider,
)


class TestChecks:
    def test_all_clean_by_default(self):
        assert check_scope_provider(None) == []
        assert check_reminder_policy(None) == []
        assert check_reminder_offsets(None) == []

    def test_bad_scope_provider_is_error(self, settings):
        settings.STAPEL_CALENDAR = {"SCOPE_PROVIDER": "nonexistent.module.Provider"}
        errors = check_scope_provider(None)
        assert errors and errors[0].id == "stapel_calendar.E001"

    def test_scope_provider_wrong_type_is_error(self, settings):
        settings.STAPEL_CALENDAR = {"SCOPE_PROVIDER": "builtins.dict"}
        errors = check_scope_provider(None)
        assert errors and errors[0].id == "stapel_calendar.E002"

    def test_bad_reminder_policy_is_warning(self, settings):
        settings.STAPEL_CALENDAR = {"REMINDER_POLICY": "nonexistent.module.Policy"}
        warnings = check_reminder_policy(None)
        assert warnings and warnings[0].id == "stapel_calendar.W001"

    def test_bad_offsets_is_error(self, settings):
        settings.STAPEL_CALENDAR = {"REMINDER_OFFSETS": [-5, "x"]}
        errors = check_reminder_offsets(None)
        assert errors and errors[0].id == "stapel_calendar.E003"
