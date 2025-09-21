from django.db import migrations


def delete_tv_events(apps, schema_editor):
    """Delete all events where the item's media_type is 'tv'."""
    Event = apps.get_model("events", "Event")
    Item = apps.get_model("app", "Item")

    # Get all items with media_type 'tv'
    tv_items = Item.objects.filter(media_type="tv")

    # Delete all events associated with these items
    Event.objects.filter(item__in=tv_items).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0007_event_notification_sent"),
    ]

    operations = [
        migrations.RunPython(delete_tv_events, migrations.RunPython.noop),
    ]
