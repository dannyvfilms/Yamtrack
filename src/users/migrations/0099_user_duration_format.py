from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0098_add_statistics_compare_mode_preference"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="duration_format",
            field=models.CharField(
                choices=[
                    ("hours_minutes", "Hours and minutes (500h 30min)"),
                    ("long_units", "Days and hours (20d 20h 30min)"),
                ],
                default="hours_minutes",
                help_text="How long durations are displayed on the Statistics page",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("duration_format__in", ["hours_minutes", "long_units"]),
                ),
                name="duration_format_valid",
            ),
        ),
    ]
