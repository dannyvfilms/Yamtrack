from django.contrib.auth import get_user_model
from django.test import TestCase

from app.helpers import get_collection_stats, get_user_collection, is_item_collected
from app.models import CollectionEntry, Item, MediaTypes, Sources


class CollectionHelpersTest(TestCase):
    """Test collection helper functions."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.movie_item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )

        self.tv_item = Item.objects.create(
            media_id="tv1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV",
            image="http://example.com/tv.jpg",
        )

    def test_get_user_collection_all(self):
        """Test get_user_collection returns all entries."""
        CollectionEntry.objects.create(user=self.user, item=self.movie_item)
        CollectionEntry.objects.create(user=self.user, item=self.tv_item)

        collection = get_user_collection(self.user)
        self.assertEqual(collection.count(), 2)

    def test_get_user_collection_filtered(self):
        """Test get_user_collection with media_type filter."""
        CollectionEntry.objects.create(user=self.user, item=self.movie_item)
        CollectionEntry.objects.create(user=self.user, item=self.tv_item)

        collection = get_user_collection(self.user, media_type=MediaTypes.MOVIE.value)
        self.assertEqual(collection.count(), 1)
        self.assertEqual(collection.first().item.media_type, MediaTypes.MOVIE.value)

    def test_is_item_collected_found(self):
        """Test is_item_collected returns CollectionEntry when found."""
        entry = CollectionEntry.objects.create(user=self.user, item=self.movie_item)

        result = is_item_collected(self.user, self.movie_item)
        self.assertEqual(result, entry)

    def test_is_item_collected_returns_latest_when_multiple_exist(self):
        """Test is_item_collected returns most recent copy for an item."""
        first_entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.movie_item,
            media_type="dvd",
        )
        latest_entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.movie_item,
            media_type="bluray",
        )

        result = is_item_collected(self.user, self.movie_item)
        self.assertNotEqual(result, first_entry)
        self.assertEqual(result, latest_entry)

    def test_is_item_collected_not_found(self):
        """Test is_item_collected returns None when not found."""
        result = is_item_collected(self.user, self.movie_item)
        self.assertIsNone(result)

    def test_get_collection_stats(self):
        """Test get_collection_stats returns correct statistics."""
        CollectionEntry.objects.create(
            user=self.user,
            item=self.movie_item,
            media_type="bluray",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=self.tv_item,
            media_type="dvd",
        )

        stats = get_collection_stats(self.user)

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_media_type"][MediaTypes.MOVIE.value], 1)
        self.assertEqual(stats["by_media_type"][MediaTypes.TV.value], 1)
        self.assertEqual(stats["by_format"]["bluray"], 1)
        self.assertEqual(stats["by_format"]["dvd"], 1)
