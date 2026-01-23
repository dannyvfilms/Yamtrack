from django.contrib.auth import get_user_model
from django.test import TestCase

from app.helpers import is_item_collected
from app.models import (
    Anime,
    Book,
    CollectionEntry,
    Comic,
    Game,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Music,
    Sources,
    Status,
    TV,
)


class CollectionIntegrationTest(TestCase):
    """Test collection operations across all media types."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def test_collection_entry_movie(self):
        """Test creating collection entry for Movie."""
        item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_tv(self):
        """Test creating collection entry for TV."""
        item = Item.objects.create(
            media_id="tv1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV",
            image="http://example.com/tv.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_anime(self):
        """Test creating collection entry for Anime."""
        item = Item.objects.create(
            media_id="anime1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/anime.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_manga(self):
        """Test creating collection entry for Manga."""
        item = Item.objects.create(
            media_id="manga1",
            source=Sources.MAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Test Manga",
            image="http://example.com/manga.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_game(self):
        """Test creating collection entry for Game."""
        item = Item.objects.create(
            media_id="game1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_book(self):
        """Test creating collection entry for Book."""
        item = Item.objects.create(
            media_id="book1",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Test Book",
            image="http://example.com/book.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_comic(self):
        """Test creating collection entry for Comic."""
        item = Item.objects.create(
            media_id="comic1",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
            title="Test Comic",
            image="http://example.com/comic.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_entry_music(self):
        """Test creating collection entry for Music."""
        item = Item.objects.create(
            media_id="music1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Test Music",
            image="http://example.com/music.jpg",
        )

        entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_independent_of_media_tracking(self):
        """Test collection entry doesn't interfere with Media tracking."""
        item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )

        # Create collection entry without Media tracking
        collection_entry = CollectionEntry.objects.create(user=self.user, item=item)
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())
        self.assertFalse(Movie.objects.filter(user=self.user, item=item).exists())

        # Create Media tracking without collection entry
        movie = Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
        )
        self.assertTrue(Movie.objects.filter(user=self.user, item=item).exists())
        # Collection entry should still exist
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=item).exists())

    def test_collection_persists_when_media_deleted(self):
        """Test collection entry persists when Media is deleted."""
        item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )

        collection_entry = CollectionEntry.objects.create(user=self.user, item=item)
        movie = Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
        )

        # Delete Media
        movie.delete()

        # Collection entry should still exist
        self.assertTrue(CollectionEntry.objects.filter(id=collection_entry.id).exists())

    def test_collection_deleted_when_item_deleted(self):
        """Test collection entry deleted when Item is deleted (CASCADE)."""
        item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )

        collection_entry = CollectionEntry.objects.create(user=self.user, item=item)

        # Delete Item
        item.delete()

        # Collection entry should be deleted
        self.assertFalse(CollectionEntry.objects.filter(id=collection_entry.id).exists())
