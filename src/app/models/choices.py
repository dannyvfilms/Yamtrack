from django.db import models


class Sources(models.TextChoices):
    """Choices for the source of the item."""

    TMDB = "tmdb", "The Movie Database"
    TVDB = "tvdb", "TheTVDB"
    MAL = "mal", "MyAnimeList"
    MANGAUPDATES = "mangaupdates", "MangaUpdates"
    IGDB = "igdb", "Internet Game Database"
    OPENLIBRARY = "openlibrary", "Open Library"
    HARDCOVER = "hardcover", "Hardcover"
    COMICVINE = "comicvine", "Comic Vine"
    BGG = "bgg", "BoardGameGeek"
    MUSICBRAINZ = "musicbrainz", "MusicBrainz"
    POCKETCASTS = "pocketcasts", "Pocket Casts"
    GPODDER = "gpodder", "GPodder"
    AUDIOBOOKSHELF = "audiobookshelf", "Audiobookshelf"
    MANUAL = "manual", "Manual"


class MediaTypes(models.TextChoices):
    """Choices for the media type of the item."""

    TV = "tv", "TV Show"
    SEASON = "season", "TV Season"
    EPISODE = "episode", "Episode"
    MOVIE = "movie", "Movie"
    ANIME = "anime", "Anime"
    MANGA = "manga", "Manga"
    GAME = "game", "Game"
    BOOK = "book", "Book"
    COMIC = "comic", "Comic"
    COMIC_ISSUE = "comicissue", "Comic Issue"
    BOARDGAME = "boardgame", "Board Game"
    MUSIC = "music", "Music"
    PODCAST = "podcast", "Podcast"


class ProviderMetadataStatus(models.TextChoices):
    """Flags for provider metadata states that need UI handling."""

    LOCAL_ONLY_MISSING_SEASON = (
        "local_only_missing_season",
        "Local only: missing season metadata",
    )


class Status(models.TextChoices):
    """Choices for item status."""

    COMPLETED = "Completed", "Completed"
    IN_PROGRESS = "In progress", "In Progress"
    PLANNING = "Planning", "Planning"
    PAUSED = "Paused", "Paused"
    DROPPED = "Dropped", "Dropped"
