from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0013_rename_integration_user_id_d7f810_idx_integration_user_id_dd5a02_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="GPodderAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("server_url", models.TextField(help_text="Encrypted GPodder-compatible server URL")),
                ("username", models.TextField(help_text="Encrypted username for HTTP Basic authentication")),
                ("password", models.TextField(help_text="Encrypted password or app password for HTTP Basic authentication")),
                ("device_id", models.CharField(help_text="Yamtrack-managed GPodder device identifier", max_length=255)),
                ("device_filter", models.CharField(blank=True, default="", help_text="Optional upstream device filter for imported actions", max_length=255)),
                ("episode_actions_since", models.BigIntegerField(blank=True, help_text="Last successfully imported GPodder episode actions cursor", null=True)),
                ("subscription_since", models.BigIntegerField(blank=True, help_text="Reserved for future incremental subscription sync", null=True)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("connection_broken", models.BooleanField(default=False)),
                ("last_error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(on_delete=models.deletion.CASCADE, related_name="gpodder_account", to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={
                "verbose_name": "GPodder account",
                "verbose_name_plural": "GPodder accounts",
            },
        ),
    ]
