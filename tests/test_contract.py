"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-calendar emits its **own** contract triad — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (generate_flow_docs machine
artifact — empty here, calendar has no ``@flow_step`` annotations) and
``docs/errors.json`` (generate_error_keys registry) — from a single-module
``{calendar + core}`` Django instance mounted at the canonical
``/calendar/api/`` prefix.

Unlike auth/profiles, **calendar is not yet mounted in
stapel-example-monolith** (grep-confirmed: no ``urls.py`` under
stapel-example-monolith references ``stapel_calendar``), so there is no
aggregate slice to diff against for byte-identity. Standalone validation
(contract-pipeline.md §9 fallback) substitutes:

  - determinism — two independent emissions are byte-identical
    (``test_emission_is_deterministic``);
  - self-contained ``$ref`` closure — every ``#/components/schemas/...``
    reference reachable from a path resolves within this one document, zero
    dangling refs (``test_schema_refs_are_self_contained``);
  - security on protected endpoints — every operation (all calendar views
    require ``IsAuthenticated``) carries the ``JWTCookieAuth`` security
    requirement (``test_protected_paths_carry_jwt_security``);
  - canonical-prefix paths — schema/flow paths are mounted at
    ``/calendar/api/*``, not bare (``test_paths_carry_canonical_prefix``).

``test_matches_monolith_calendar_slice`` is wired for the day calendar *is*
mounted there (mirrors auth/profiles) but is unconditionally skipped today —
there is no slice yet, so there is nothing to hand-tune against; do not fake
one.

Regenerate after any change to a serializer / view / url / flow / error key:

    make contract        # or: python -m stapel_calendar._codegen --out docs

then commit ``docs/{schema,flows,errors}.json``. Without regenerating, the drift
gate below fails — the same byte-stable regenerate-and-diff discipline as
``test_error_keys``.

The harness runs in a **subprocess**: this test process already configured Django
(via conftest, on the bare test urlconf), and the harness needs its own
canonical-prefix urlconf + drf-spectacular singleton — a clean interpreter is the
honest way to exercise exactly what ``make contract`` runs.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    _PY312_MSG = (
        "stapel-calendar contract tests require Python 3.12 (the "
        f"CI/monolith pin) — running {_GOT}. drf-spectacular renders "
        "component descriptions (Optional[X] vs X | None) differently "
        "across Python minor versions, so drift/identity checks "
        "emitted+compared under any other minor produce false diffs."
    )
    pytest.skip(
        _PY312_MSG + " Skipping on any non-3.12 interpreter (CI or local) — "
        "the contract canon is only defined on Python 3.12.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
# The fourth artifact (capability-config.md §2): calendar's settings are all
# tuning knobs or extension seams, so the manifest carries axes: [] —
# provides/requires/extension_points still feed the capability catalog.
# Emitted from conf.py DEFAULTS + the urls.py gate registry + schema.json +
# the curated docs/capabilities.meta.json. Same emit/drift discipline.
ARTIFACTS = TRIAD + ("capabilities.json",)


def _emit(out_dir: Path) -> None:
    for module in ("stapel_calendar._codegen", "stapel_calendar._capabilities"):
        subprocess.run(
            [sys.executable, "-m", module, "--out", str(out_dir)],
            cwd=str(REPO),
            check=True,
            capture_output=True,
        )


def test_contract_artifacts_committed():
    for name in ARTIFACTS:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"
    assert (DOCS / "capabilities.meta.json").is_file(), (
        "missing docs/capabilities.meta.json — the curated layer is "
        "hand-written and committed, not generated"
    )


def test_contract_has_no_drift(tmp_path):
    """Regenerate into a temp dir; committed artifacts must match byte-for-byte."""
    _emit(tmp_path)
    for name in ARTIFACTS:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    """The mount-prefix fix: schema paths + flow endpoints are /calendar/api/*, not bare."""
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/calendar/api/") for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /calendar/api/ prefix"
    )
    flows = json.loads((DOCS / "flows.json").read_text())
    for flow in flows:
        for step in flow.get("steps", []):
            for ep in step.get("endpoints", []):
                assert ep["path"].startswith("/calendar/api/"), (
                    f"flow endpoint {ep['path']} is not canonically prefixed"
                )


def test_flows_are_empty_no_flow_step_annotations():
    """calendar has no @flow_step annotations — [] is the correct, not a missing, artifact."""
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == [], (
        "docs/flows.json is non-empty but no @flow_step annotation exists in "
        "stapel_calendar — investigate before assuming [] is still correct"
    )


# --- Standalone validation (contract-pipeline.md §9 fallback: no monolith slice) --
# calendar is not mounted in stapel-example-monolith yet, so there is no aggregate
# to diff byte-for-byte. These three checks are the substitute gate.


def _all_refs(obj) -> set[str]:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_refs_are_self_contained():
    """Every $ref reachable from a path resolves inside this one schema.json.

    A module harness that mounts {module + core} alone must not emit a $ref to
    a component that only exists when a sibling module is also installed
    (contract-pipeline.md §9 Q2). Zero dangling refs confirms calendar's
    schema is a closed, self-sufficient document.
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    comps = schema.get("components", {}).get("schemas", {})

    seeds = _all_refs(schema["paths"])
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in comps:
            stack.extend(_all_refs(comps[name]))

    dangling = seen - set(comps)
    assert not dangling, f"dangling $ref(s) with no component definition: {dangling}"


def test_protected_paths_carry_jwt_security():
    """Every calendar operation requires IsAuthenticated; schema must say so.

    Without the explicit _register_jwt_auth_extension() call in _codegen.py,
    drf-spectacular has no JWTCookieAuth extension registered and every
    operation would silently emit with no `security` entry at all — a real
    contract gap a frontend client can't detect from the schema alone.
    """
    schema = json.loads((DOCS / "schema.json").read_text())
    missing = []
    for path, operations in schema["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            security = op.get("security") or []
            if not any("JWTCookieAuth" in entry for entry in security):
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"operations missing JWTCookieAuth security: {missing}"


# --- Byte-identity regression vs the monolith aggregate's calendar slice ------
# Dormant: calendar is not mounted in stapel-example-monolith today (grep-confirmed
# — no urls.py there references stapel_calendar). Wired for the day it is, mirroring
# auth/profiles; unconditionally skipped until then. Do not fabricate a slice.

_MONO = REPO.parent / "stapel-example-monolith" / "codegen" / "generated" / "schema.json"


def _closure(schema: dict, seeds: set[str]) -> set[str]:
    comps = schema["components"]["schemas"]
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen or name not in comps:
            continue
        seen.add(name)
        stack.extend(_all_refs(comps[name]))
    return seen


@pytest.mark.skipif(
    True,
    reason="stapel-calendar is not mounted in stapel-example-monolith yet — "
    "no aggregate slice exists to compare against (contract-pipeline.md §9 fallback: "
    "see the standalone validation tests above instead)",
)
def test_matches_monolith_calendar_slice():
    """docs/schema.json == the monolith aggregate's /calendar/api/ slice, byte-for-byte.

    Compares path objects and the transitive component closure — the envelope
    (info/servers) is intentionally not compared (it names calendar, not the
    monolith). Activate once svc-app/core/urls.py mounts
    ``path("calendar/api/", include("stapel_calendar.urls"))``.
    """
    assert _MONO.exists(), "monolith aggregate not present"
    mine = json.loads((DOCS / "schema.json").read_text())
    mono = json.loads(_MONO.read_text())

    mono_paths = {p: v for p, v in mono["paths"].items() if p.startswith("/calendar/api/")}
    assert set(mine["paths"]) == set(mono_paths), "path set differs from monolith slice"
    for p in mono_paths:
        assert json.dumps(mine["paths"][p], sort_keys=True) == json.dumps(
            mono_paths[p], sort_keys=True
        ), f"path object {p} differs from monolith slice"

    seeds: set[str] = set()
    for v in mono_paths.values():
        seeds |= _all_refs(v)
    mono_cl = _closure(mono, seeds)
    my_seeds: set[str] = set()
    for v in mine["paths"].values():
        my_seeds |= _all_refs(v)
    my_cl = _closure(mine, my_seeds)
    assert mono_cl == my_cl, "component closure differs from monolith slice"
    for c in mono_cl:
        assert json.dumps(mine["components"]["schemas"][c], sort_keys=True) == json.dumps(
            mono["components"]["schemas"][c], sort_keys=True
        ), f"component {c} differs from monolith slice"


# --- capabilities.json content sanity (capability-config.md §2) ---------------


def _capabilities() -> dict:
    return json.loads((DOCS / "capabilities.json").read_text())


def test_capabilities_axes_empty_by_design():
    """All STAPEL_CALENDAR keys are tuning or extension seams → axes: []."""
    assert _capabilities()["axes"] == []


def test_capabilities_extension_points_cover_the_seams():
    """The three settings-level seams (MODULE.md) surface as extension points."""
    names = {e["name"] for e in _capabilities()["extension_points"]}
    assert {"REMINDER_POLICY", "SCOPE_PROVIDER", "PRESETS"} <= names


def test_capabilities_operations_total_matches_schema():
    schema = json.loads((DOCS / "schema.json").read_text())
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    total = sum(
        1 for item in schema["paths"].values() for m in item if m in methods
    )
    assert _capabilities()["operations_total"] == total


def test_capabilities_envelope():
    doc = _capabilities()
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert doc["module"] == pyproject["project"]["name"]
    assert doc["version"] == pyproject["project"]["version"]
    assert doc["provides"]
    assert doc["extension_points"]
    assert doc["requires"]


def test_capabilities_stale_meta_axis_fails_loudly():
    """A curated axis entry for a module with no axes must be an emission ERROR."""
    from stapel_tools.capabilities import build_capabilities

    from stapel_calendar.conf import DEFAULTS
    from stapel_calendar.urls import GATE_REGISTRY

    schema = json.loads((DOCS / "schema.json").read_text())
    meta = json.loads((DOCS / "capabilities.meta.json").read_text())
    broken = json.loads(json.dumps(meta))
    broken["axes"]["CALENDAR_NO_SUCH_AXIS"] = {"summary": "x", "business_label": "x"}

    with pytest.raises(SystemExit, match="CALENDAR_NO_SUCH_AXIS"):
        build_capabilities(
            module="stapel-calendar",
            version="0.0.0",
            defaults=DEFAULTS,
            registry=GATE_REGISTRY,
            schema=schema,
            meta=broken,
            is_axis=lambda k: False,
            axis_group=lambda k: "unreachable",
            canonical_prefix="/calendar",
        )
