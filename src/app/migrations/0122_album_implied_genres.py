from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0121_albumartist_credited_artists"),
    ]

    operations = [
        migrations.AddField(
            model_name="album",
            name="implied_genres",
            field=models.JSONField(blank=True, default=list, help_text="Derived parent genres from MusicBrainz genre relationships"),
        ),
    ]
