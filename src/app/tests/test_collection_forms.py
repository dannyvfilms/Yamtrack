from django.contrib.auth import get_user_model
from django.test import TestCase

from app.forms import CollectionEntryForm
from app.models import CollectionEntry, Item, MediaTypes, Sources


class CollectionEntryFormTest(TestCase):
    """Test CollectionEntryForm."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_form_validation_valid_data(self):
        """Test form validation with valid data."""
        form = CollectionEntryForm(
            {
                "item": self.item.id,
                "media_type": "bluray",
                "resolution": "1080p",
                "hdr": "HDR10",
                "is_3d": False,
                "audio_codec": "DTS",
                "audio_channels": "5.1",
            },
            user=self.user,
        )

        self.assertTrue(form.is_valid())

    def test_form_validation_optional_fields_blank(self):
        """Test optional metadata fields can be blank."""
        form = CollectionEntryForm(
            {
                "item": self.item.id,
            },
            user=self.user,
        )

        self.assertTrue(form.is_valid())

    def test_form_validation_invalid_item(self):
        """Test item validation (must exist)."""
        form = CollectionEntryForm(
            {
                "item": 99999,  # Non-existent ID
            },
            user=self.user,
        )

        # Form should not validate with non-existent item
        # Django's ModelChoiceField will validate that the item exists
        self.assertFalse(form.is_valid())
        self.assertIn("item", form.errors)

    def test_form_save_creates_entry(self):
        """Test form save creates CollectionEntry."""
        form = CollectionEntryForm(
            {
                "item": self.item.id,
                "media_type": "bluray",
            },
            user=self.user,
        )

        self.assertTrue(form.is_valid())
        entry = form.save(commit=False)
        entry.user = self.user
        entry.item = self.item
        entry.save()

        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=self.item).exists())
