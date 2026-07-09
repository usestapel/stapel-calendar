"""stapel-calendar capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_calendar._codegen import _configure

    _configure()
    from stapel_calendar.conf import DEFAULTS
    from stapel_calendar.urls import GATE_REGISTRY

    # One CTO-facing axis: VISIBILITY (capability-config.md §16 —
    # participants|scope). It is behavioral, not gating: it does not unmount
    # any endpoint (no URL-factory flag), it widens/narrows what an endpoint
    # returns — so gates.operations stays empty and the semantics live in the
    # curated behavior/summary of docs/capabilities.meta.json. Every OTHER
    # DEFAULTS key is a tuning knob (expansion horizon/caps, reminder
    # offsets/scan window, slot length) or an extension seam (REMINDER_POLICY /
    # SCOPE_PROVIDER dotted paths, PRESETS merge-registry).
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/calendar",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k == "VISIBILITY",
        axis_group=axis_group_rules(exact={"VISIBILITY": "calendar.visibility"}),
        prog="stapel-calendar-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
