"""Backfill engagements whose `status` or `engagement_type` is NULL/empty.

Neither column offers an empty choice and both declare a default, so an empty value
carries no meaning. Because Django counts None among a field's empty values, such a row
failed its own validation on every save ("This field cannot be blank."), which made every
scan ingest into that engagement fail. `Engagement.pre_save_logic` normalizes the value
going forward; this backfill repairs the rows already stored so they recover without
waiting for something to save them.

Reversible as a no-op: the pre-fix state is corrupt data, not a schema the fix depends on.
"""
from django.db import migrations
from django.db.models import Q


def backfill_empty_status(apps, schema_editor):
    engagement_model = apps.get_model("dojo", "Engagement")
    empty = Q(status__isnull=True) | Q(status="")
    engagement_model.objects.filter(empty).update(status="Not Started")
    empty_type = Q(engagement_type__isnull=True) | Q(engagement_type="")
    engagement_model.objects.filter(empty_type).update(engagement_type="Interactive")


class Migration(migrations.Migration):

    dependencies = [
        ("dojo", "0280_vulnerability_id_upper_index"),
    ]

    operations = [
        migrations.RunPython(backfill_empty_status, migrations.RunPython.noop),
    ]
