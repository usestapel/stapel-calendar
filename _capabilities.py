"""stapel-calendar capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import run_capabilities_cli


def _no_group(key: str) -> str:
    raise SystemExit(f"capabilities: stapel-calendar has no axes, got key {key!r}")


def main(argv=None):
    from stapel_calendar._codegen import _configure

    _configure()
    from stapel_calendar.conf import DEFAULTS
    from stapel_calendar.urls import GATE_REGISTRY

    # No CTO-facing axes: every DEFAULTS key is either a tuning knob
    # (expansion horizon/caps, reminder offsets/scan window, slot length) or
    # an extension seam (REMINDER_POLICY / SCOPE_PROVIDER dotted paths,
    # PRESETS merge-registry — curated in docs/capabilities.meta.json).
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/calendar",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: False,
        axis_group=_no_group,
        prog="stapel-calendar-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
