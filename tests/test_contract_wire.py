"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* the operations that genuinely cannot be driven in-process are listed by
  name in ``UNDRIVABLE`` with a one-line reason each. That list is asserted
  to be exactly current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing. Here that covers the nested collections too
  (``events``/``occurrences``/``busy``/``slots``/``participants``), which a
  generic "is the body a list" check cannot see;
* every read is driven a SECOND time in its emptiest legal state
  (``EMPTY_STATE``): a calendar with nothing in it, an availability read for
  somebody with no windows and no meetings, an event carrying none of its
  optional values. Every null finding in the first wave of this gate was
  there.

Runs on every interpreter: it reads the committed schema and never emits.
``tests/test_contract.py`` skips off Python 3.12 because it re-emits; this
file never does, so it has no interpreter opinion.

THE MOUNT. ``codegen_urls.py`` mounts ``calendar/`` → ``stapel_calendar.urls``,
which contributes ``api/v1/``, so the document is written against
``/calendar/api/v1/…``. Calendar is one of the libraries whose test urlconf
(``tests/urls.py``) already mounts EXACTLY that — the suite here has always
been looking where the document points. The emission mount is nonetheless
declared in this module rather than borrowed, so that
``test_every_declared_path_resolves_under_this_urlconf`` fails at the one
moment it is cheap to fix: when somebody changes a mount.

What it found on its first run — 9 of 9 operations driven, 1 red:

* ``DELETE /calendar/api/v1/events/{event_id}`` declares ``EventResponse``
  (ten REQUIRED properties: ``id``, ``title``, ``start``, ``end``,
  ``owner_id``, ``status``, …) and answers ``{"status": "deleted"}`` — or
  ``{"status": "cancelled"}`` on the tombstone branch — and nothing else. The
  body is deliberate and correct behaviour (``views.py`` ``EventDetailView.
  delete``: a standalone event is deleted, a materialized occurrence is
  tombstoned so the rule instant does not resurrect); what is wrong is the
  annotation ``@extend_schema(responses={200: EventResponseSerializer})``
  directly above it, copied from the GET/PATCH halves of the same class. A
  generated client reads ``body.id`` after a delete and gets ``undefined``;
  worse, ``body.status`` exists on BOTH shapes and means an event status
  (confirmed/tentative/cancelled) in the declared one and an outcome word in
  the real one, so a client that switches on it cannot tell the two apart.
  Recorded in ``KNOWN_MISMATCHES`` and left exactly as it is: this is a gate,
  not a fix.

The other eight are honest, including every ``nullable`` claim, in both the
populated and the empty state. ``test_the_gate_is_not_blind`` proves that is
a finding rather than a gate that never looked: it re-validates every driven
body against ``{"type": "string"}`` and requires all of them to fail.
"""
import copy
import json
import re
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``calendar/`` → ``stapel_calendar.urls``, which
#: contributes ``api/v1/``).
urlpatterns = [
    url_path("calendar/", include("stapel_calendar.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/calendar/api/v1"

UTC = ZoneInfo("UTC")

#: A fixed Monday, so the weekday of an ``AvailabilityWindow`` is not a
#: function of the day the suite happens to run on.
DAY = datetime(2026, 1, 5, tzinfo=UTC)
RANGE_START = DAY
RANGE_END = DAY + timedelta(days=7)
#: ``+00:00`` in a query string decodes to a SPACE, which ``parse_datetime``
#: rejects and the view answers 400 for — so the offset is spelled ``Z``.
RANGE = (
    f"?start={RANGE_START.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    f"&end={RANGE_END.strftime('%Y-%m-%dT%H:%M:%SZ')}"
)


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Nothing here writes files today; pin the root so nothing ever does.

    ``MEDIA_ROOT`` is unset in this module's harness settings
    (``_codegen_settings.py``), so it defaults to the working directory — in
    stapel-auth that put a data export into the checkout, where under a flat
    package layout a stray directory also shadowed a real submodule.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergence that matters here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null — which is
    exactly what ``recurrence_parent_id`` and ``materialized_id`` answer in
    the state this gate most wants to ask about. Everything else
    drf-spectacular emits here (``$ref``, ``required``, ``format``) is JSON
    Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user(**kwargs):
    from django.contrib.auth import get_user_model

    defaults = dict(
        username=_unique("wire_"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )
    defaults.update(kwargs)
    return get_user_model().objects.create_user(**defaults)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_event(owner, *, offset_hours=10, duration_hours=1, **kwargs):
    """A plain, concrete event on the fixed Monday."""
    from stapel_calendar import services

    defaults = dict(
        title="Wire contract sync",
        start=DAY + timedelta(hours=offset_hours),
        end=DAY + timedelta(hours=offset_hours + duration_hours),
    )
    defaults.update(kwargs)
    return services.create_event(owner=owner, **defaults)


def make_series(owner, **kwargs):
    """A recurring master — the only source of ``occurrences``."""
    return make_event(owner, recurrence_type="daily", offset_hours=14, **kwargs)


def make_window(user, weekday=0, start_hour=9, end_hour=17):
    from stapel_calendar.models import AvailabilityWindow

    return AvailabilityWindow.objects.create(
        user=user,
        weekday=weekday,
        start_time=time(start_hour, 0),
        end_time=time(end_hour, 0),
        timezone="UTC",
    )


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template)``. A recipe returns the response it
#: produced, or a list of ``(label, response)`` pairs when one operation has
#: more than one answering branch — every pair is validated.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe: nothing in the calendar, no availability windows, an event
#: carrying none of its optional values. A populated answer cannot say what a
#: field holds when there is nothing to hold, and that is where every null
#: finding in the first wave of this gate was.
EMPTY_STATE = {}


def recipe(method, path, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path)
        assert key not in target, f"duplicate recipe for {method} {path}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path):
    return recipe(method, path, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares with a JSON body is reachable
#: from a test client. (``GET /events/{event_id}/ics`` is not in this table
#: because it is not in the table above either: it answers ``text/calendar``
#: and the contract declares no ``application/json`` body for it.)
UNDRIVABLE: dict = {}

#: Collections nested inside an object body that must actually carry a row in
#: the populated pass. An empty array validates against any item schema, so a
#: populated run that leaves one empty looked at nothing — and the generic
#: "is the body a list" check cannot see a list one level down.
POPULATED_COLLECTIONS = {
    ("GET", V1 + "/calendar"): ("events", "occurrences"),
    ("GET", V1 + "/availability"): ("busy", "slots"),
    ("GET", V1 + "/events/{event_id}"): ("participants",),
    ("POST", V1 + "/events"): ("participants",),
    ("PATCH", V1 + "/events/{event_id}"): ("participants",),
    ("PUT", V1 + "/events/{event_id}/participants"): ("participants",),
    ("POST", V1 + "/events/{event_id}/respond"): ("participants",),
}


# ── the event list ───────────────────────────────────────────────────────────


@recipe("GET", "/events")
def _events_list(call):
    owner = make_user()
    make_event(owner)
    make_series(owner)
    return call(client_for(owner), query=RANGE)


@empty_state("GET", "/events")
def _events_list_empty(call):
    """Nobody's calendar: the default ``participants`` visibility filters to
    the caller's own events, and this caller is on none."""
    return call(client_for(make_user()), query=RANGE)


@recipe("POST", "/events")
def _events_create(call):
    owner = make_user()
    invitee = make_user()
    return call(
        client_for(owner),
        data={
            "title": "Wire contract kickoff",
            "description": "Created by the wire gate",
            "start": (DAY + timedelta(hours=9)).isoformat(),
            "end": (DAY + timedelta(hours=10)).isoformat(),
            "recurrence_type": "weekly",
            "participant_ids": [str(invitee.id)],
        },
    )


@empty_state("POST", "/events")
def _events_create_empty(call):
    """The minimum a create accepts: no description, no recurrence, no
    invitees — so ``rrule`` is the empty string and ``recurrence_parent_id``
    is null, the two fields most likely to be a lie in this shape."""
    return call(
        client_for(make_user()),
        data={
            "title": "Bare",
            "start": (DAY + timedelta(hours=9)).isoformat(),
            "end": (DAY + timedelta(hours=10)).isoformat(),
        },
    )


# ── one event ────────────────────────────────────────────────────────────────


@recipe("GET", "/events/{event_id}")
def _event_get(call):
    owner = make_user()
    event = make_event(
        owner,
        description="A described event",
        participant_ids=[str(make_user().id)],
        recurrence_type="weekly",
    )
    return call(client_for(owner), params={"event_id": event.id})


@empty_state("GET", "/events/{event_id}")
def _event_get_empty(call):
    """An event carrying none of its optional values: blank description,
    blank scope_key, no rrule, no recurrence parent, owner-only participants."""
    owner = make_user()
    event = make_event(owner)
    return call(client_for(owner), params={"event_id": event.id})


@recipe("PATCH", "/events/{event_id}")
def _event_patch(call):
    owner = make_user()
    event = make_event(owner, recurrence_type="weekly")
    return call(
        client_for(owner),
        params={"event_id": event.id},
        data={"title": "Renamed by the wire gate", "description": "And described"},
    )


@empty_state("PATCH", "/events/{event_id}")
def _event_patch_empty(call):
    """A rename on an event carrying none of its optional values: the
    declared answer for ``description`` is the empty string, not null, and
    ``rrule`` likewise — ``recurrence_parent_id`` is the null."""
    owner = make_user()
    event = make_event(owner)
    return call(
        client_for(owner), params={"event_id": event.id}, data={"title": "Still bare"}
    )


@recipe("DELETE", "/events/{event_id}")
def _event_delete(call):
    """Both branches of the delete: a standalone row removed, and a
    materialized occurrence tombstoned (the EXDATE analog)."""
    from stapel_calendar import services

    owner = make_user()
    standalone = make_event(owner)
    series = make_series(owner)
    occurrence = services.materialize(series, series.start + timedelta(days=1))
    return [
        (
            "a standalone event (deleted)",
            call(client_for(owner), params={"event_id": standalone.id}),
        ),
        (
            "a materialized occurrence (tombstoned)",
            call(client_for(owner), params={"event_id": occurrence.id}),
        ),
    ]


# ── participants and RSVP ────────────────────────────────────────────────────


@recipe("PUT", "/events/{event_id}/participants")
def _participants_replace(call):
    owner = make_user()
    event = make_event(owner)
    return call(
        client_for(owner),
        params={"event_id": event.id},
        data={"participant_ids": [str(make_user().id), str(make_user().id)]},
    )


@empty_state("PUT", "/events/{event_id}/participants")
def _participants_replace_empty(call):
    """An empty replace-set: everybody uninvited. The owner is retained by
    design, so this is the smallest participant list that can exist."""
    owner = make_user()
    event = make_event(owner, participant_ids=[str(make_user().id)])
    return call(
        client_for(owner), params={"event_id": event.id}, data={"participant_ids": []}
    )


@recipe("POST", "/events/{event_id}/respond")
def _event_respond(call):
    owner = make_user()
    invitee = make_user()
    event = make_event(owner, participant_ids=[str(invitee.id)])
    return call(
        client_for(invitee), params={"event_id": event.id}, data={"rsvp": "accepted"}
    )


@empty_state("POST", "/events/{event_id}/respond")
def _event_respond_empty(call):
    """The owner answering their own bare event: no description, no
    recurrence, nobody else on it."""
    owner = make_user()
    event = make_event(owner)
    return call(
        client_for(owner), params={"event_id": event.id}, data={"rsvp": "declined"}
    )


# ── the calendar and the availability reads ──────────────────────────────────


@recipe("GET", "/calendar")
def _calendar(call):
    """Concrete events AND occurrences, one of them materialized — so
    ``materialized_id`` is a real id in one row and null in the rest."""
    from stapel_calendar import services

    owner = make_user()
    make_event(owner)
    series = make_series(owner)
    services.materialize(series, series.start + timedelta(days=1))
    return call(client_for(owner), query=RANGE)


@empty_state("GET", "/calendar")
def _calendar_empty(call):
    return call(client_for(make_user()), query=RANGE)


@recipe("GET", "/availability")
def _availability(call):
    """A booked hour and the free hours around it: ``busy`` carries the
    meeting, ``slots`` carries what is left of the working window."""
    owner = make_user()
    make_window(owner)
    make_event(owner, offset_hours=10, duration_hours=1)
    return call(client_for(owner), query=RANGE + "&slot_minutes=60")


@empty_state("GET", "/availability")
def _availability_empty(call):
    """No windows and no meetings: ``busy`` and ``slots`` are both empty and
    ``truncated`` is the boolean the contract promises, not null."""
    return call(client_for(make_user()), query=RANGE + "&slot_minutes=60")


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send, with the defect and
#: its owner. ``strict=True``: a fixed entry fails until it is deleted, so a
#: finding can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES = {
    ("DELETE", V1 + "/events/{event_id}"):
        "declares EventResponse — ten REQUIRED properties, id/title/start/"
        "end/owner_id/status among them — and answers {\"status\": "
        "\"deleted\"} for a standalone event and {\"status\": \"cancelled\"} "
        "for a materialized occurrence, and nothing else. Both bodies are "
        "deliberate (views.py, EventDetailView.delete: the row is removed, or "
        "tombstoned so the rule instant does not resurrect); the annotation "
        "@extend_schema(responses={200: EventResponseSerializer}) above it "
        "was copied from the get/patch halves of the same class and never "
        "corrected. A generated client reads body.id after a delete and gets "
        "undefined — and `status` exists on BOTH shapes with different "
        "meanings (an event status vs an outcome word), so a client cannot "
        "even tell them apart. Owner: stapel-calendar.",
}


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, one mounted a
    doubled segment, one mounted less than the emission did. In every case
    the operations were "covered" by a file that could not have reached a
    single one of them.

    Calendar is not one of them — ``tests/urls.py`` and ``codegen_urls.py``
    mount the identical ``calendar/`` prefix — and this assertion is what
    keeps saying so. A missing recipe already fails loudly; this fails when
    the MOUNT is wrong, which no per-operation check can see, because when
    the mount is wrong every operation is equally and silently unreachable.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and a urlconf may use
    # several converters — uuid, int, slug. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    covered = set(RECIPES) | set(UNDRIVABLE)

    missing = sorted(declared - covered)
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - declared)
    assert not stale, (
        "recipes/exclusions for operations the contract no longer declares:\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )
    both = sorted(set(RECIPES) & set(UNDRIVABLE))
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    stale_collections = sorted(set(POPULATED_COLLECTIONS) - declared)
    assert not stale_collections, (
        f"nested-collection expectations for undeclared operations: {stale_collections}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.
    """
    reads = {
        (method, path)
        for method, path, _code, _schema in OPERATIONS
        if method == "GET"
    }
    missing = sorted(reads - set(EMPTY_STATE))
    assert not missing, (
        "reads driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared = {(m, p) for m, p, _c, _s in OPERATIONS}
    stale = sorted(set(EMPTY_STATE) - declared)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


def _labelled(result):
    """A recipe answers with one response, or with labelled branches."""
    if isinstance(result, list):
        return result
    return [("", result)]


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = table.get((method, path))
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    for label, response in _labelled(perform(Call(method, path))):
        where = f"{method} {path}" + (f" [{label}]" if label else "")
        assert response.status_code == code, (
            f"{where}: expected the declared {code}, got "
            f"{response.status_code}: {response.content[:400]}"
        )

        body = response.json()
        errors = sorted(
            _validator(body_schema).iter_errors(body), key=lambda e: list(e.path)
        )
        assert not errors, (
            f"{where} answers a body the contract does not describe:\n"
            + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
            + f"\n  body: {json.dumps(body)[:600]}"
        )

        # An empty list validates against any item schema, so a collection
        # must actually carry a row for the check to have looked at anything.
        if expect_rows:
            if isinstance(body, list):
                assert body, f"{where}: the declared list came back empty"
            for name in POPULATED_COLLECTIONS.get((method, path), ()):
                assert isinstance(body, dict) and body.get(name), (
                    f"{where}: the declared collection {name!r} came back "
                    "empty, so nothing in it was checked"
                )


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p}" for m, p, _c, _s in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if (method, path) in EMPTY_STATE
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p}" for m, p, _c, _s in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object or an array, so every one of them must fail. If any passes, the
    validation in ``_drive`` is not reaching the received body and this whole
    file proves nothing.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
