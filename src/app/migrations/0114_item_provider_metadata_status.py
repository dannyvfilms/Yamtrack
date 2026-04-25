from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0113_widen_podcastepisode_audio_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="provider_metadata_status",
            field=models.CharField(
                blank=True,
                choices=[
                    (
                        "local_only_missing_season",
                        "Local only: missing season metadata",
                    ),
                ],
                default="",
                help_text="Flags special provider metadata states for UI and recovery flows",
                max_length=64,
            ),
        ),
    ]
