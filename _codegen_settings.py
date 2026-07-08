"""Single-module Django settings for stapel-calendar's harnesses.

Single source of truth for the ``settings.configure(...)`` block shared by:

  - the pytest suite (``conftest.py``) — mounts calendar on its *bare* test
    urlconf (``stapel_calendar.tests.urls`` -> ``calendar/`` -> the module's own
    ``api/events`` etc., the historical test layout); and
  - the contract-emission harness (``_codegen.py`` / ``make contract``) — mounts
    calendar on its *canonical* public API prefix (``stapel_calendar.codegen_urls``
    -> ``calendar/`` -> same ``api/*`` paths the module's own ``urls.py`` already
    declares) and enables drf-spectacular, so the emitted ``schema.json`` /
    ``flows.json`` paths are byte-identical to what a host mounting this module
    would serve (contract-pipeline.md §2).

Keeping one copy here means the harness and the tests can never drift in their
``INSTALLED_APPS`` / mock config — the exact hazard contract-pipeline.md §3
calls out ("~30 lines that *reference* the already-existing config, not a
second copy of it"). Copied from stapel-auth's etalon (``_codegen_settings.py``)
and stapel-profiles' adaptation; tailored to this module's actual conftest
content (no gdpr/social_django/JWT/Twilio — calendar carries none of that, but
it does need the in-process comm bus + schema validation the existing conftest
already configures).
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_calendar.tests.urls",
    contract: bool = False,
) -> dict:
    """Return the ``settings.configure(**kwargs)`` for a single-module
    calendar instance.

    ``root_urlconf`` selects the mount: bare (``stapel_calendar.tests.urls``,
    which itself mounts ``calendar/`` -> ``stapel_calendar.urls``) for the test
    suite, canonical-prefix (``stapel_calendar.codegen_urls``, same ``calendar/``
    mount) for contract emission — the module's own ``urls.py`` already bakes
    ``api/`` into every path, so both urlconfs resolve to identical paths; the
    codegen one exists only to add drf-spectacular into the mix.

    ``contract=True`` swaps in the *production* ``REST_FRAMEWORK`` (the
    canonical stapel-core config, inlined as plain dotted paths — importing it
    would trip the same chicken-and-egg as spectacular). This matters for
    byte-identity: a host wiring calendar for real uses
    ``DEFAULT_SCHEMA_CLASS=PermissionAwareAutoSchema`` and the real
    permission/renderer classes, and DRF caches ``REST_FRAMEWORK`` on first
    access, so it must be right at ``configure()`` time — a post-hoc
    assignment is too late. The test suite keeps its historical config
    (``contract=False``, no ``REST_FRAMEWORK`` key at all — DRF's own
    defaults, matching the conftest before this extraction).

    ``SPECTACULAR_SETTINGS`` is deliberately *not* set. drf-spectacular builds
    its settings singleton at *import* time (``getattr(settings,
    'SPECTACULAR_SETTINGS', {})`` at module load), before a
    ``configure()``-based harness can populate it, so a Django-level
    ``SPECTACULAR_SETTINGS`` is silently ignored and the emitter runs on drf
    **defaults** (``info.title=""``, no ``x-stapel-*`` extensions) — same as
    every other pair-backend's harness. The one knob that still must be
    forced, ``SCHEMA_PATH_PREFIX``, is patched on the singleton directly by
    the harness (see ``_codegen._configure``).
    """
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real host mounts calendar under). Inlined, not imported, to
        # dodge the import-time settings read; kept in lockstep by
        # test_contract.py's identity gate.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            # CommonDjangoConfig ships the stapel_core management commands
            # (generate_error_keys, used by the errors.json drift gate).
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_calendar",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Synchronous in-process comm with schema validation ON, so the
        # committed contracts in schemas/ are enforced by the tests.
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
        },
        # Skip migrations — create tables directly from models
        MIGRATION_MODULES={
            "users": None,
            "calendar": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects in a
# multi-module aggregate (see stapel-auth's etalon note). Forced on the
# drf-spectacular settings singleton by the harness so a single-module
# instance derives the same style of operationIds (see _codegen._configure
# and the SCHEMA_PATH_PREFIX note above). Uniform across all pair-backends.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
