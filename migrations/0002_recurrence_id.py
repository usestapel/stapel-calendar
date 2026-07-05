"""Add Event.recurrence_id (RFC 5545 RECURRENCE-ID analog) and re-key the
occurrence uniqueness constraint from (recurrence_parent, start) to
(recurrence_parent, recurrence_id), so a rescheduled occurrence keeps
claiming its original rule instant. Existing occurrence rows are backfilled
with recurrence_id = start (they were created at their rule instant)."""
from django.db import migrations, models


def backfill_recurrence_id(apps, schema_editor):
    Event = apps.get_model("calendar", "Event")
    Event.objects.filter(
        recurrence_parent__isnull=False, recurrence_id__isnull=True
    ).update(recurrence_id=models.F("start"))


class Migration(migrations.Migration):

    dependencies = [
        ("calendar", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="event",
            name="cal_event_uniq_occurrence",
        ),
        migrations.AddField(
            model_name="event",
            name="recurrence_id",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_recurrence_id, migrations.RunPython.noop
        ),
        migrations.AddConstraint(
            model_name="event",
            constraint=models.UniqueConstraint(
                condition=models.Q(("recurrence_parent__isnull", False)),
                fields=("recurrence_parent", "recurrence_id"),
                name="cal_event_uniq_occurrence",
            ),
        ),
    ]
