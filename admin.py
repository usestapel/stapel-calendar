"""Admin registrations for stapel-calendar (observability; kept minimal)."""
from django.contrib import admin

from .models import AvailabilityWindow, Event, Participant


class ParticipantInline(admin.TabularInline):
    model = Participant
    extra = 0
    fields = ("user", "rsvp")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "start", "end", "owner", "scope_key", "recurrence_type")
    list_filter = ("recurrence_type", "status")
    search_fields = ("title", "scope_key")
    date_hierarchy = "start"
    inlines = [ParticipantInline]


@admin.register(AvailabilityWindow)
class AvailabilityWindowAdmin(admin.ModelAdmin):
    list_display = ("user", "weekday", "start_time", "end_time", "timezone")
    list_filter = ("weekday",)
