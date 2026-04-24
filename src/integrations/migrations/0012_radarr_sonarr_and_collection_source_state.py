from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0102_itemproviderlink_metadataproviderpreference_and_more"),
        ("integrations", "0011_lastfmaccount_history_import_completed_at_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RadarrAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("base_url", models.URLField(help_text="Radarr server URL")),
                ("api_key", models.TextField(help_text="Encrypted Radarr API key")),
                ("connection_broken", models.BooleanField(default=False)),
                ("last_error_message", models.TextField(blank=True, default="")),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="radarr_account",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Radarr account",
                "verbose_name_plural": "Radarr accounts",
            },
        ),
        migrations.CreateModel(
            name="SonarrAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("base_url", models.URLField(help_text="Sonarr server URL")),
                ("api_key", models.TextField(help_text="Encrypted Sonarr API key")),
                ("connection_broken", models.BooleanField(default=False)),
                ("last_error_message", models.TextField(blank=True, default="")),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sonarr_account",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Sonarr account",
                "verbose_name_plural": "Sonarr accounts",
            },
        ),
        migrations.CreateModel(
            name="CollectionSourceState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("plex", "Plex"), ("radarr", "Radarr"), ("sonarr", "Sonarr")], max_length=20)),
                ("quality_label", models.CharField(blank=True, default="", max_length=80)),
                ("last_source_updated_at", models.DateTimeField(blank=True, null=True)),
                ("last_synced_at", models.DateTimeField(auto_now=True)),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_states",
                        to="app.item",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="collection_source_states",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="collectionsourcestate",
            constraint=models.UniqueConstraint(
                fields=("user", "item", "source"),
                name="integrations_collectionsourcestate_unique_user_item_source",
            ),
        ),
        migrations.AddIndex(
            model_name="collectionsourcestate",
            index=models.Index(fields=["user", "source"], name="integration_user_id_d7f810_idx"),
        ),
        migrations.AddIndex(
            model_name="collectionsourcestate",
            index=models.Index(fields=["user", "item"], name="integration_user_id_942e5a_idx"),
        ),
    ]
