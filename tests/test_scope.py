"""scope_key provider — extension seam #3."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stapel_calendar import services
from stapel_calendar.models import Event
from stapel_calendar.scope import DefaultScopeProvider, ScopeProvider, get_scope_provider

UTC = ZoneInfo("UTC")


class WorkspaceScopeProvider(ScopeProvider):
    """Example app-layer provider: scope = request.workspace_id."""

    def resolve(self, request):
        return getattr(request, "workspace_id", "")

    def filter(self, queryset, request):
        return queryset.filter(scope_key=getattr(request, "workspace_id", ""))


class TestScopeSeam:
    def test_default_provider_is_noop(self, settings):
        provider = get_scope_provider()
        assert isinstance(provider, DefaultScopeProvider)
        assert provider.resolve(object()) == ""

    def test_custom_provider_resolves(self, settings):
        settings.STAPEL_CALENDAR = {
            "SCOPE_PROVIDER": "stapel_calendar.tests.test_scope.WorkspaceScopeProvider"
        }
        provider = get_scope_provider()
        # Compare by name: pytest's importlib mode can give the test module a
        # different identity than the dotted-path import, so `isinstance`
        # against the local class object is unreliable.
        assert type(provider).__name__ == "WorkspaceScopeProvider"

        class Req:
            workspace_id = "ws-1"

        assert provider.resolve(Req()) == "ws-1"

    @pytest.mark.django_db
    def test_custom_provider_filters(self, settings, user):
        settings.STAPEL_CALENDAR = {
            "SCOPE_PROVIDER": "stapel_calendar.tests.test_scope.WorkspaceScopeProvider"
        }
        services.create_event(
            owner=user, title="A", scope_key="ws-1",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        )
        services.create_event(
            owner=user, title="B", scope_key="ws-2",
            start=datetime(2026, 1, 5, 9, tzinfo=UTC),
            end=datetime(2026, 1, 5, 10, tzinfo=UTC),
        )

        class Req:
            workspace_id = "ws-1"

        provider = get_scope_provider()
        visible = provider.filter(Event.objects.all(), Req())
        assert [e.title for e in visible] == ["A"]
