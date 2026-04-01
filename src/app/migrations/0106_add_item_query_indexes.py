"""Add indexes on Item fields used by background tasks and media list sorting."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0105_episode_score"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="item",
            index=models.Index(
                fields=["metadata_fetched_at"],
                name="app_item_metadata_fetched_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="item",
            index=models.Index(
                fields=["release_datetime"],
                name="app_item_release_dt_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="item",
            index=models.Index(
                fields=["trakt_popularity_rank"],
                name="app_item_trakt_pop_rank_idx",
            ),
        ),
    ]
