# Adds the "storyteller" source to the Item/provider source constraint.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0126_alter_item_source_alter_itemproviderlink_provider_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='item',
            name='app_item_source_valid',
        ),
        migrations.AlterField(
            model_name='item',
            name='source',
            field=models.CharField(choices=[('tmdb', 'The Movie Database'), ('tvdb', 'TheTVDB'), ('mal', 'MyAnimeList'), ('mangaupdates', 'MangaUpdates'), ('igdb', 'Internet Game Database'), ('openlibrary', 'Open Library'), ('hardcover', 'Hardcover'), ('comicvine', 'Comic Vine'), ('bgg', 'BoardGameGeek'), ('musicbrainz', 'MusicBrainz'), ('pocketcasts', 'Pocket Casts'), ('gpodder', 'GPodder'), ('audiobookshelf', 'Audiobookshelf'), ('storyteller', 'Storyteller'), ('manual', 'Manual')], max_length=20),
        ),
        migrations.AlterField(
            model_name='itemproviderlink',
            name='provider',
            field=models.CharField(choices=[('tmdb', 'The Movie Database'), ('tvdb', 'TheTVDB'), ('mal', 'MyAnimeList'), ('mangaupdates', 'MangaUpdates'), ('igdb', 'Internet Game Database'), ('openlibrary', 'Open Library'), ('hardcover', 'Hardcover'), ('comicvine', 'Comic Vine'), ('bgg', 'BoardGameGeek'), ('musicbrainz', 'MusicBrainz'), ('pocketcasts', 'Pocket Casts'), ('gpodder', 'GPodder'), ('audiobookshelf', 'Audiobookshelf'), ('storyteller', 'Storyteller'), ('manual', 'Manual')], max_length=20),
        ),
        migrations.AlterField(
            model_name='metadataproviderpreference',
            name='provider',
            field=models.CharField(choices=[('tmdb', 'The Movie Database'), ('tvdb', 'TheTVDB'), ('mal', 'MyAnimeList'), ('mangaupdates', 'MangaUpdates'), ('igdb', 'Internet Game Database'), ('openlibrary', 'Open Library'), ('hardcover', 'Hardcover'), ('comicvine', 'Comic Vine'), ('bgg', 'BoardGameGeek'), ('musicbrainz', 'MusicBrainz'), ('pocketcasts', 'Pocket Casts'), ('gpodder', 'GPodder'), ('audiobookshelf', 'Audiobookshelf'), ('storyteller', 'Storyteller'), ('manual', 'Manual')], max_length=20),
        ),
        migrations.AlterField(
            model_name='person',
            name='source',
            field=models.CharField(choices=[('tmdb', 'The Movie Database'), ('tvdb', 'TheTVDB'), ('mal', 'MyAnimeList'), ('mangaupdates', 'MangaUpdates'), ('igdb', 'Internet Game Database'), ('openlibrary', 'Open Library'), ('hardcover', 'Hardcover'), ('comicvine', 'Comic Vine'), ('bgg', 'BoardGameGeek'), ('musicbrainz', 'MusicBrainz'), ('pocketcasts', 'Pocket Casts'), ('gpodder', 'GPodder'), ('audiobookshelf', 'Audiobookshelf'), ('storyteller', 'Storyteller'), ('manual', 'Manual')], default='tmdb', max_length=20),
        ),
        migrations.AlterField(
            model_name='studio',
            name='source',
            field=models.CharField(choices=[('tmdb', 'The Movie Database'), ('tvdb', 'TheTVDB'), ('mal', 'MyAnimeList'), ('mangaupdates', 'MangaUpdates'), ('igdb', 'Internet Game Database'), ('openlibrary', 'Open Library'), ('hardcover', 'Hardcover'), ('comicvine', 'Comic Vine'), ('bgg', 'BoardGameGeek'), ('musicbrainz', 'MusicBrainz'), ('pocketcasts', 'Pocket Casts'), ('gpodder', 'GPodder'), ('audiobookshelf', 'Audiobookshelf'), ('storyteller', 'Storyteller'), ('manual', 'Manual')], default='tmdb', max_length=20),
        ),
        migrations.AddConstraint(
            model_name='item',
            constraint=models.CheckConstraint(condition=models.Q(('source__in', ['tmdb', 'tvdb', 'mal', 'mangaupdates', 'igdb', 'openlibrary', 'hardcover', 'comicvine', 'bgg', 'musicbrainz', 'pocketcasts', 'gpodder', 'audiobookshelf', 'storyteller', 'manual'])), name='app_item_source_valid'),
        ),
    ]
