"""stapel-calendar contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{calendar + core}`` Django instance mounted at the canonical
``calendar/api/`` prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact ([] — no @flow_step here)
  docs/errors.json   generate_error_keys registry

Copied from stapel-auth's reference implementation (``_codegen.py``,
ETALON) and stapel-profiles' adaptation; the *mechanism* is
stapel_tools.codegen (unchanged, shared), this file is the thin per-module
*config* that wires the module's settings + canonical mount into it.

Usage:
    python -m stapel_calendar._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    # `python -m` prepends cwd to sys.path; strip the repo root the same way
    # conftest.py does (defensively — calendar has no colliding subpackage
    # today, but this mirrors auth/profiles so the guard is never missing if
    # one is ever added).
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_calendar._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_calendar.codegen_urls", contract=True)
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time (before this
    # harness ran configure()), so it is on drf defaults. The one knob to force
    # is SCHEMA_PATH_PREFIX: left None, drf derives the operationId prefix from
    # the common path of all endpoints — "/" across a multi-module aggregate
    # (operationIds keep the mount segment, calendar_api_*), but
    # "/calendar/api" in a single-module harness (which would strip it to bare
    # anonymous names). Pin it to the aggregate-style common prefix so the
    # operationIds match the convention every other pair-backend uses;
    # SCHEMA_PATH_PREFIX_TRIM stays False (default) so the path *keys* keep
    # /calendar/api/ on both sides.
    from drf_spectacular.settings import spectacular_settings

    from stapel_calendar._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # A real multi-module host registers drf-spectacular's JWT cookie-auth
    # extension as a side effect of wiring its dev-only Swagger URLs
    # (get_dev_urls -> get_swagger_urls -> _register_jwt_auth_extension()) —
    # a *global* registration on drf-spectacular's extension registry, not
    # tied to any one module's urls.py. stapel-auth's harness gets this for
    # free only because its co-mounted sibling (stapel_gdpr.urls) happens to
    # call get_app_swagger_urls() unconditionally; calendar has no such
    # sibling (this module mounts alone — see codegen_urls.py). Without
    # registering it explicitly here, calendar's protected endpoints (every
    # view requires IsAuthenticated) would emit without their
    # `security: [{"JWTCookieAuth": []}]` entry — a real contract gap, not a
    # component-closure gap (contract-pipeline.md §9 Q2 is about $ref'd
    # schemas; this is about a side-effecting extension registration a real
    # host always triggers). Applied per stapel-profiles' precedent.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stapel-calendar-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /calendar/api/ prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    # Reuse the shared mechanism's byte-stable emitters (contract-pipeline.md §2:
    # "the single-module harness already exists").
    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-calendar contract: {paths} paths, {flows} flows, {errors} error keys "
        f"→ {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
