# Generated manually on 2026-02-15

from django.db import migrations


def set_null_themes_to_empty_list(apps, schema_editor):
    Item = apps.get_model("app", "Item")
    Item.objects.filter(themes__isnull=True).update(themes=[])


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0088_fix_themes_null_constraint"),
    ]

    operations = [
        migrations.RunPython(
            set_null_themes_to_empty_list, reverse_code=migrations.RunPython.noop
        ),
    ]
