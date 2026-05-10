from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Fix PodcastEpisode.episode_uuid:
    - Increase max_length from 36 to 500 (RSS GUIDs are often URL strings, not UUID4s)
    - Change unique constraint from global to per-show (RSS GUIDs are only unique within
      a single feed; a global unique constraint causes IntegrityError when two shows
      happen to share any GUID, silently blocking all remaining episodes for the second show)
    """

    dependencies = [
        ("app", "0115_backfill_music_and_podcast_history_users"),
    ]

    operations = [
        migrations.AlterField(
            model_name="podcastepisode",
            name="episode_uuid",
            field=models.CharField(
                help_text="Pocket Casts episode UUID or RSS GUID",
                max_length=500,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="podcastepisode",
            unique_together={("show", "episode_uuid")},
        ),
    ]
