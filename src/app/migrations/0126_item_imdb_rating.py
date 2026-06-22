from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0125_fix_episode_season_library_bucket"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="imdb_rating",
            field=models.FloatField(
                blank=True,
                help_text="Average rating value from IMDB",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="imdb_rating_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Rating count from IMDB",
                null=True,
            ),
        ),
    ]
