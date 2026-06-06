"""
Migration: convert quick_season_update_mobile from BooleanField to a four-way
CharField (none / season_update / next_episode / both).

Strategy: add the new CharField alongside the old BooleanField, migrate data
with RunPython, then drop the old field and rename the new one.  This avoids
relying on the database to coerce boolean → varchar transparently.
"""

from django.db import migrations, models


def bool_to_choice(apps, schema_editor):
    User = apps.get_model("users", "User")
    # True  (had quick-update buttons) → "season_update"
    User.objects.filter(quick_season_update_mobile_old=True).update(
        quick_season_update_mobile="season_update"
    )
    # False (disabled) → "none"
    User.objects.filter(quick_season_update_mobile_old=False).update(
        quick_season_update_mobile="none"
    )


def choice_to_bool(apps, schema_editor):
    """Reverse: any value that implied buttons → True, otherwise False."""
    User = apps.get_model("users", "User")
    User.objects.filter(quick_season_update_mobile__in=["season_update", "both"]).update(
        quick_season_update_mobile_old=True
    )
    User.objects.exclude(quick_season_update_mobile__in=["season_update", "both"]).update(
        quick_season_update_mobile_old=False
    )


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0099_user_duration_format"),
    ]

    operations = [
        # 1. Rename the existing BooleanField so we can reuse the original name
        migrations.RenameField(
            model_name="user",
            old_name="quick_season_update_mobile",
            new_name="quick_season_update_mobile_old",
        ),
        # 2. Add the new CharField with a temporary default
        migrations.AddField(
            model_name="user",
            name="quick_season_update_mobile",
            field=models.CharField(
                choices=[
                    ("none", "None"),
                    ("season_update", "Quick Season Update buttons only"),
                    ("next_episode", "Next Episode button only"),
                    ("both", "Both"),
                ],
                default="none",
                help_text="Controls quick season update buttons and next-episode pill on home screen cards",
                max_length=20,
            ),
        ),
        # 3. Populate the new field from the old boolean
        migrations.RunPython(bool_to_choice, reverse_code=choice_to_bool),
        # 4. Drop the old boolean field
        migrations.RemoveField(
            model_name="user",
            name="quick_season_update_mobile_old",
        ),
    ]
