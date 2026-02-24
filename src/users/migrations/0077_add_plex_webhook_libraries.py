from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0076_add_release_date_to_list_detail_sort"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="plex_webhook_libraries",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text="List of Plex webhook library keys to accept. Null means all available libraries.",
                null=True,
            ),
        ),
    ]
