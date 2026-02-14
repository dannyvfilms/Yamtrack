from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import CollectionEntry, Item, MediaTypes, Sources


class CollectionEntryModelTest(TestCase):
    """Test case for the CollectionEntry model."""

    def setUp(self):
        """Set up test data for CollectionEntry model tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_collection_entry_creation(self):
        """Test the creation of a CollectionEntry instance."""
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="bluray",
            resolution="1080p",
            hdr="HDR10",
            is_3d=False,
            audio_codec="DTS",
            audio_channels="5.1",
        )

        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.item, self.item)
        self.assertEqual(entry.media_type, "bluray")
        self.assertEqual(entry.resolution, "1080p")
        self.assertEqual(entry.hdr, "HDR10")
        self.assertFalse(entry.is_3d)
        self.assertEqual(entry.audio_codec, "DTS")
        self.assertEqual(entry.audio_channels, "5.1")

    def test_collection_entry_timestamps(self):
        """Test that timestamps are auto-set and auto-updated."""
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )

        # Check collected_at is set
        self.assertIsNotNone(entry.collected_at)
        self.assertAlmostEqual(
            entry.collected_at,
            timezone.now(),
            delta=timezone.timedelta(seconds=5),
        )

        # Check updated_at is set
        self.assertIsNotNone(entry.updated_at)

        # Update entry and check updated_at changes
        old_updated_at = entry.updated_at
        entry.media_type = "dvd"
        entry.save()

        # updated_at should be newer
        entry.refresh_from_db()
        self.assertGreater(entry.updated_at, old_updated_at)

    def test_collection_entry_string_representation(self):
        """Test the string representation of a CollectionEntry."""
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )
        self.assertEqual(str(entry), f"{self.user.username} - {self.item.title}")

    def test_collection_entry_allows_multiple_entries_per_item(self):
        """Test that multiple owned copies can be stored for the same item."""
        first_entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="dvd",
        )

        second_entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="bluray",
        )

        self.assertNotEqual(first_entry.id, second_entry.id)
        self.assertEqual(CollectionEntry.objects.filter(user=self.user, item=self.item).count(), 2)

    def test_collection_entry_field_defaults(self):
        """Test that all metadata fields have correct defaults."""
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )

        self.assertEqual(entry.media_type, "")
        self.assertEqual(entry.resolution, "")
        self.assertEqual(entry.hdr, "")
        self.assertFalse(entry.is_3d)
        self.assertEqual(entry.audio_codec, "")
        self.assertEqual(entry.audio_channels, "")

    def test_collection_entry_ordering(self):
        """Test that collection entries are ordered by collected_at descending."""
        item2 = Item.objects.create(
            media_id="5678",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie 2",
            image="http://example.com/image2.jpg",
        )

        entry1 = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )
        entry2 = CollectionEntry.objects.create(
            user=self.user,
            item=item2,
        )

        entries = list(CollectionEntry.objects.filter(user=self.user))
        # Most recent should be first
        self.assertEqual(entries[0], entry2)
        self.assertEqual(entries[1], entry1)

    def test_collection_entry_cascade_on_item_delete(self):
        """Test that collection entry is deleted when item is deleted."""
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )

        self.item.delete()

        # Entry should be deleted
        self.assertFalse(CollectionEntry.objects.filter(id=entry.id).exists())

    def test_collection_entry_cascade_on_user_delete(self):
        """Test that collection entry is deleted when user is deleted."""
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )

        self.user.delete()

        # Entry should be deleted
        self.assertFalse(CollectionEntry.objects.filter(id=entry.id).exists())

    def test_collection_entry_field_max_lengths(self):
        """Test that field max lengths are enforced."""
        media_type_max = CollectionEntry._meta.get_field("media_type").max_length
        resolution_max = CollectionEntry._meta.get_field("resolution").max_length
        hdr_max = CollectionEntry._meta.get_field("hdr").max_length
        audio_codec_max = CollectionEntry._meta.get_field("audio_codec").max_length
        audio_channels_max = CollectionEntry._meta.get_field("audio_channels").max_length

        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="a" * media_type_max,
            resolution="a" * resolution_max,
            hdr="a" * hdr_max,
            audio_codec="a" * audio_codec_max,
            audio_channels="a" * audio_channels_max,
        )

        # Should save successfully
        self.assertEqual(len(entry.media_type), media_type_max)
        self.assertEqual(len(entry.resolution), resolution_max)
        self.assertEqual(len(entry.hdr), hdr_max)
        self.assertEqual(len(entry.audio_codec), audio_codec_max)
        self.assertEqual(len(entry.audio_channels), audio_channels_max)
