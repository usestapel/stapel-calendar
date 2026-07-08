"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

stapel-calendar's own ``urls.py`` already bakes ``api/`` into every path
(``api/events``, ``api/calendar``, ``api/availability``, ...) and documents its
own expected host mount in its module docstring::

    path("calendar/", include("stapel_calendar.urls"))

That is the canonical public API prefix (``calendar/api/...``) — the same
``<mod>/api/`` shape every other pair-backend uses (``auth/api/``,
``profiles/api/``, ...), just assembled from ``calendar/`` (host mount) +
``api/`` (module-internal) instead of a single ``calendar/api/`` literal in
one urls.py. This harness urlconf reproduces exactly that documented mount, so
drf-spectacular emits ``/calendar/api/...`` paths and ``generate_flow_docs``
resolves flow endpoints to the same.

Unlike stapel-auth (co-mounts ``stapel_gdpr``) this module has no sibling
mounted under the same host prefix — calendar is validated standalone (no
monolith slice exists yet to diff against; contract-pipeline.md §9 fallback
path applies until calendar is mounted there).
"""
from django.conf.urls import include
from django.urls import path

urlpatterns = [
    path("calendar/", include("stapel_calendar.urls")),
]
