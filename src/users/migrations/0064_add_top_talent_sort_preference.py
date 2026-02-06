from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0063_add_plex_webhook_status_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="top_talent_sort_by",
            field=models.CharField(
                choices=[("plays", "Plays"), ("time", "Time"), ("titles", "Titles")],
                default="plays",
                help_text="Sort metric for top cast/crew/studio cards on the Statistics page",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("top_talent_sort_by__in", ["plays", "time", "titles"])),
                name="top_talent_sort_by_valid",
            ),
        ),
    ]
