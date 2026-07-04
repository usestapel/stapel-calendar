"""scope_key provider — extension seam #3.

The library is scope-agnostic: ``Event.scope_key`` is an opaque string the
host owns. A ``ScopeProvider`` (dotted path in
``STAPEL_CALENDAR["SCOPE_PROVIDER"]``) resolves the scope_key from the
current request and filters querysets by it. The default is a no-op single
global scope; legacy's provider returns the active ``workspace_id`` and
filters events to that workspace.
"""
from __future__ import annotations


class ScopeProvider:
    """Contract for scope resolution/filtering. Subclass and point
    ``STAPEL_CALENDAR["SCOPE_PROVIDER"]`` at it to scope the calendar."""

    def resolve(self, request) -> str:
        """Return the scope_key to stamp on events created via ``request``."""
        raise NotImplementedError

    def filter(self, queryset, request):
        """Restrict ``queryset`` to the scope visible to ``request``."""
        raise NotImplementedError


class DefaultScopeProvider(ScopeProvider):
    """Single global scope: every event gets ``scope_key=""`` and no query
    is filtered by scope. Suitable for single-tenant hosts and tests."""

    def resolve(self, request) -> str:
        return ""

    def filter(self, queryset, request):
        return queryset


def get_scope_provider() -> ScopeProvider:
    """Resolve the configured provider (already import_string'd by conf)."""
    from .conf import calendar_settings

    provider = calendar_settings.SCOPE_PROVIDER
    return provider() if isinstance(provider, type) else provider
