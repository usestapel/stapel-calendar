"""Serializers for the stapel-calendar API (dataclass-DTO backed).

Every view exposes request/response serializer seams (SerializerSeamMixin);
these are the defaults.
"""
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    AvailabilityResponse,
    CalendarResponse,
    EventCreateRequest,
    EventResponse,
    EventUpdateRequest,
    IntervalResponse,
    OccurrenceResponse,
    ParticipantResponse,
    ParticipantsReplaceRequest,
    RSVPRequest,
)


class ParticipantResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ParticipantResponse


class EventResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = EventResponse


class OccurrenceResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = OccurrenceResponse


class CalendarResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CalendarResponse


class IntervalResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = IntervalResponse


class AvailabilityResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = AvailabilityResponse


class EventCreateRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = EventCreateRequest


class RSVPRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RSVPRequest


class EventUpdateRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = EventUpdateRequest


class ParticipantsReplaceRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ParticipantsReplaceRequest
