"""DRF views for stapel-calendar.

Thin views over :mod:`services`. Scope resolution/filtering goes through the
``SCOPE_PROVIDER`` seam so the host controls what ``scope_key`` an event gets
and which events a request may see.
"""
from datetime import timedelta

from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse

from . import ics, services
from .conf import calendar_settings
from .dto import (
    AvailabilityResponse,
    CalendarResponse,
    EventResponse,
    IntervalResponse,
    OccurrenceResponse,
    ParticipantResponse,
)
from .errors import (
    ERR_400_INVALID_RANGE,
    ERR_400_INVALID_RECURRENCE,
    ERR_400_INVALID_RSVP,
    ERR_400_INVALID_SLOT_MINUTES,
    ERR_403_NOT_EVENT_OWNER,
    ERR_404_EVENT_NOT_FOUND,
    ERR_404_NOT_INVITED,
)
from .models import Event, EventStatus, Participant, RSVP
from .recurrence import InvalidRecurrence
from .scope import get_scope_provider
from .serializers import (
    AvailabilityResponseSerializer,
    CalendarResponseSerializer,
    EventCreateRequestSerializer,
    EventResponseSerializer,
    EventUpdateRequestSerializer,
    ParticipantsReplaceRequestSerializer,
    RSVPRequestSerializer,
)


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-calendar APIView.

    Host projects can swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` (or overriding the getters for
    per-request decisions) — no need to rewrite the HTTP method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


# ── Mappers ──────────────────────────────────────────────────────────────


def event_to_dto(event: Event) -> EventResponse:
    return EventResponse(
        id=str(event.id),
        title=event.title,
        description=event.description,
        start=event.start,
        end=event.end,
        owner_id=str(event.owner_id),
        scope_key=event.scope_key,
        status=event.status,
        recurrence_type=event.recurrence_type,
        rrule=event.rrule,
        recurrence_parent_id=(
            str(event.recurrence_parent_id) if event.recurrence_parent_id else None
        ),
        participants=[
            ParticipantResponse(user_id=str(p.user_id), rsvp=p.rsvp)
            for p in event.participants.all()
        ],
    )


def occurrence_to_dto(occ) -> OccurrenceResponse:
    return OccurrenceResponse(
        event_id=str(occ.event_id),
        start=occ.start,
        end=occ.end,
        is_materialized=occ.is_materialized,
        materialized_id=str(occ.materialized_id) if occ.materialized_id else None,
    )


def _parse_range(request):
    """Resolve [start, end] from query params, defaulting to now .. now +
    DEFAULT_EXPANSION_HORIZON_DAYS. Returns (start, end) or raises ValueError."""
    now = timezone.now()
    start_raw = request.query_params.get("start")
    end_raw = request.query_params.get("end")
    start = parse_datetime(start_raw) if start_raw else now
    end = (
        parse_datetime(end_raw)
        if end_raw
        else now + timedelta(days=calendar_settings.DEFAULT_EXPANSION_HORIZON_DAYS)
    )
    if start is None or end is None or end < start:
        raise ValueError("invalid range")
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    if timezone.is_naive(end):
        end = timezone.make_aware(end)
    return start, end


def _visible_events(request):
    """Base queryset of events the request may read, honoring the VISIBILITY
    axis (capability-config.md §16) and then the SCOPE_PROVIDER seam.

    - ``participants`` (default, fail-closed): only events the request user is
      a participant of — the historical behavior.
    - ``scope``: every event in the scope the provider resolves for the request
      (workspace/org/tenant-wide calendars). Any value other than ``scope``
      falls back to ``participants`` so a typo never widens visibility.

    The scope provider is applied in BOTH modes: it is what actually bounds
    ``scope`` visibility to the caller's workspace (with the default no-op
    provider, ``scope`` is a single global calendar — pair it with a real
    provider for per-workspace visibility).
    """
    qs = Event.objects.all()
    if calendar_settings.VISIBILITY != "scope":
        qs = qs.filter(participants__user=request.user)
    return get_scope_provider().filter(qs, request)


# ── Views ────────────────────────────────────────────────────────────────


@extend_schema(tags=["Calendar"])
class EventListCreateView(SerializerSeamMixin, APIView):
    """List the requesting user's events in a range, or create an event."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = EventCreateRequestSerializer
    response_serializer_class = EventResponseSerializer

    @extend_schema(responses={200: EventResponseSerializer(many=True)})
    def get(self, request):
        try:
            start, end = _parse_range(request)
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVALID_RANGE)
        qs = (
            _visible_events(request)
            .filter(start__lte=end, end__gte=start)
            .distinct()
            .prefetch_related("participants")
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls([event_to_dto(e) for e in qs], many=True)
        )

    @extend_schema(
        request=EventCreateRequestSerializer,
        responses={201: EventResponseSerializer},
    )
    def post(self, request):
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data.end < data.start:
            return StapelErrorResponse(400, ERR_400_INVALID_RANGE)
        scope_key = get_scope_provider().resolve(request)
        try:
            event = services.create_event(
                owner=request.user,
                title=data.title,
                start=data.start,
                end=data.end,
                description=data.description,
                scope_key=scope_key,
                recurrence_type=data.recurrence_type,
                recurrence_interval=data.recurrence_interval,
                recurrence_weekdays=data.recurrence_weekdays or None,
                recurrence_until=data.recurrence_until,
                recurrence_count=data.recurrence_count,
                participant_ids=data.participant_ids,
            )
        except InvalidRecurrence:
            return StapelErrorResponse(400, ERR_400_INVALID_RECURRENCE)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(event_to_dto(event)), status=status.HTTP_201_CREATED
        )


@extend_schema(tags=["Calendar"])
class EventDetailView(SerializerSeamMixin, APIView):
    """Retrieve/update/delete a single event (mutations owner-only)."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = EventUpdateRequestSerializer
    response_serializer_class = EventResponseSerializer

    #: Request-body fields the PATCH surface forwards to services.update_event.
    _UPDATABLE_FIELDS = (
        "title",
        "description",
        "start",
        "end",
        "status",
        "recurrence_type",
        "recurrence_interval",
        "recurrence_weekdays",
        "recurrence_until",
        "recurrence_count",
    )

    def _get(self, request, event_id):
        qs = get_scope_provider().filter(Event.objects.all(), request)
        return qs.prefetch_related("participants").filter(id=event_id).first()

    @extend_schema(responses={200: EventResponseSerializer})
    def get(self, request, event_id):
        event = self._get(request, event_id)
        if event is None:
            return StapelErrorResponse(404, ERR_404_EVENT_NOT_FOUND)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(event_to_dto(event)))

    @extend_schema(
        request=EventUpdateRequestSerializer,
        responses={200: EventResponseSerializer},
    )
    def patch(self, request, event_id):
        event = self._get(request, event_id)
        if event is None:
            return StapelErrorResponse(404, ERR_404_EVENT_NOT_FOUND)
        if event.owner_id != request.user.id:
            return StapelErrorResponse(403, ERR_403_NOT_EVENT_OWNER)
        ser = self.get_request_serializer_class()(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        # True PATCH: only the fields the client actually sent become changes.
        # (The dataclass DTO gives every field a default, which the serializer
        # turns into a field default — so an absent field validates to its
        # default rather than a sentinel; presence is read from the raw body.)
        present = set(request.data.keys())
        changes = {
            name: getattr(data, name)
            for name in self._UPDATABLE_FIELDS
            if name in present
        }
        try:
            event = services.update_event(event, changes)
        except InvalidRecurrence:
            return StapelErrorResponse(400, ERR_400_INVALID_RECURRENCE)
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVALID_RANGE)
        event = (
            Event.objects.prefetch_related("participants").filter(id=event.id).first()
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(event_to_dto(event)))

    @extend_schema(responses={200: EventResponseSerializer})
    def delete(self, request, event_id):
        event = self._get(request, event_id)
        if event is None:
            return StapelErrorResponse(404, ERR_404_EVENT_NOT_FOUND)
        if event.owner_id != request.user.id:
            return StapelErrorResponse(403, ERR_403_NOT_EVENT_OWNER)
        if event.is_occurrence:
            # Deleting a materialized occurrence must not resurrect the
            # virtual one at its rule instant — tombstone it (the EXDATE
            # analog) instead of removing the row.
            event.status = EventStatus.CANCELLED
            event.save(update_fields=["status", "updated_at"])
            return StapelResponse({"status": "cancelled"})
        event.delete()
        return StapelResponse({"status": "deleted"})


@extend_schema(tags=["Calendar"])
class EventRespondView(SerializerSeamMixin, APIView):
    """Record the requesting user's RSVP to an event."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = RSVPRequestSerializer
    response_serializer_class = EventResponseSerializer

    @extend_schema(
        request=RSVPRequestSerializer, responses={200: EventResponseSerializer}
    )
    def post(self, request, event_id):
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        rsvp = ser.validated_data.rsvp
        if rsvp not in (RSVP.ACCEPTED, RSVP.TENTATIVE, RSVP.DECLINED):
            return StapelErrorResponse(400, ERR_400_INVALID_RSVP)
        try:
            services.respond(Event(id=event_id), request.user, rsvp)
        except Participant.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_NOT_INVITED)
        event = (
            Event.objects.prefetch_related("participants").filter(id=event_id).first()
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(event_to_dto(event)))


@extend_schema(tags=["Calendar"])
class EventParticipantsView(SerializerSeamMixin, APIView):
    """Replace an event's participant set (replace-set semantics, owner-only)."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = ParticipantsReplaceRequestSerializer
    response_serializer_class = EventResponseSerializer

    def _get(self, request, event_id):
        qs = get_scope_provider().filter(Event.objects.all(), request)
        return qs.prefetch_related("participants").filter(id=event_id).first()

    @extend_schema(
        request=ParticipantsReplaceRequestSerializer,
        responses={200: EventResponseSerializer},
    )
    def put(self, request, event_id):
        event = self._get(request, event_id)
        if event is None:
            return StapelErrorResponse(404, ERR_404_EVENT_NOT_FOUND)
        # Managing the invitee list is an organizer action — in this engine the
        # organizer is the event owner (there is no separate organizer role).
        if event.owner_id != request.user.id:
            return StapelErrorResponse(403, ERR_403_NOT_EVENT_OWNER)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        services.replace_participants(event, ser.validated_data.participant_ids)
        event = (
            Event.objects.prefetch_related("participants").filter(id=event.id).first()
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(event_to_dto(event)))


@extend_schema(tags=["Calendar"])
class EventICSView(SerializerSeamMixin, APIView):
    """Export an event (series RRULE included) as an RFC 5545 .ics file."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, event_id):
        qs = get_scope_provider().filter(Event.objects.all(), request)
        event = qs.filter(id=event_id).first()
        if event is None:
            return StapelErrorResponse(404, ERR_404_EVENT_NOT_FOUND)
        body = ics.to_ics(event)
        resp = HttpResponse(body, content_type="text/calendar; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{event.id}.ics"'
        return resp


@extend_schema(tags=["Calendar"])
class CalendarView(SerializerSeamMixin, APIView):
    """The requesting user's calendar over a range: concrete events plus the
    virtual+materialized occurrences of every series they're on."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = CalendarResponseSerializer

    @extend_schema(responses={200: CalendarResponseSerializer})
    def get(self, request):
        try:
            start, end = _parse_range(request)
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVALID_RANGE)

        base = _visible_events(request).distinct()
        concrete = base.filter(rrule="", start__lte=end, end__gte=start).prefetch_related(
            "participants"
        )
        series = base.exclude(rrule="").prefetch_related("participants")

        occurrences = []
        for master in series:
            occurrences.extend(services.expand_event(master, start, end))

        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(
                CalendarResponse(
                    events=[event_to_dto(e) for e in concrete],
                    occurrences=[occurrence_to_dto(o) for o in occurrences],
                )
            )
        )


@extend_schema(tags=["Calendar"])
class AvailabilityView(SerializerSeamMixin, APIView):
    """Free/busy + open booking slots for the requesting user over a range."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = AvailabilityResponseSerializer

    @extend_schema(responses={200: AvailabilityResponseSerializer})
    def get(self, request):
        try:
            start, end = _parse_range(request)
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVALID_RANGE)
        slot_raw = request.query_params.get("slot_minutes")
        slot_minutes = None
        if slot_raw:
            # Reject non-numeric, zero and negative values up front — a
            # step <= 0 would make the slot loop run forever (DoS).
            try:
                slot_minutes = int(slot_raw)
            except (TypeError, ValueError):
                return StapelErrorResponse(400, ERR_400_INVALID_SLOT_MINUTES)
            if slot_minutes < 1:
                return StapelErrorResponse(400, ERR_400_INVALID_SLOT_MINUTES)

        fb = services.free_busy_detailed(request.user, start, end)
        slot_result = services.compute_slots_detailed(
            request.user, start, end, slot_minutes=slot_minutes
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(
                AvailabilityResponse(
                    busy=[
                        IntervalResponse(start=i.start, end=i.end) for i in fb.busy
                    ],
                    slots=[
                        IntervalResponse(start=i.start, end=i.end)
                        for i in slot_result.slots
                    ],
                    truncated=fb.truncated or slot_result.truncated,
                )
            )
        )
