from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0077_add_plex_webhook_libraries"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="authenticator_confirmed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Timestamp when authenticator setup was confirmed",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="authenticator_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Whether authenticator app verification is enabled",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="authenticator_secret",
            field=models.CharField(
                blank=True,
                default="",
                help_text="TOTP secret used by authenticator apps",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="UserRecoveryCode",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("code_hash", models.CharField(db_index=True, max_length=64)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recovery_codes",
                        to="users.user",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
