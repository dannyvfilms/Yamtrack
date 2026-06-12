from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0123_item_implied_genres"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="item",
            name="app_item_source_valid",
        ),
        migrations.AddConstraint(
            model_name="item",
            constraint=models.CheckConstraint(
                condition=Q(
                    source__in=[
                        "tmdb",
                        "tvdb",
                        "mal",
                        "mangaupdates",
                        "igdb",
                        "openlibrary",
                        "hardcover",
                        "comicvine",
                        "bgg",
                        "musicbrainz",
                        "pocketcasts",
                        "gpodder",
                        "audiobookshelf",
                        "manual",
                    ],
                ),
                name="app_item_source_valid",
            ),
        ),
        migrations.AddField(
            model_name="podcastshow",
            name="source",
            field=models.CharField(
                choices=[
                    ("tmdb", "The Movie Database"),
                    ("tvdb", "TheTVDB"),
                    ("mal", "MyAnimeList"),
                    ("mangaupdates", "MangaUpdates"),
                    ("igdb", "Internet Game Database"),
                    ("openlibrary", "Open Library"),
                    ("hardcover", "Hardcover"),
                    ("comicvine", "Comic Vine"),
                    ("bgg", "BoardGameGeek"),
                    ("musicbrainz", "MusicBrainz"),
                    ("pocketcasts", "Pocket Casts"),
                    ("gpodder", "GPodder"),
                    ("audiobookshelf", "Audiobookshelf"),
                    ("manual", "Manual"),
                ],
                default="pocketcasts",
                help_text="Podcast provider source for this show",
                max_length=20,
            ),
        ),
    ]
