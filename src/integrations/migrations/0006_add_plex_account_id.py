"""Add Plex account id field."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add Plex account id to PlexAccount."""

    dependencies = [
        ("integrations", "0005_alter_pocketcastsaccount_refresh_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="plexaccount",
            name="plex_account_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
