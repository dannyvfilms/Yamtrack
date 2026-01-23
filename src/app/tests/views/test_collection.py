from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from app.helpers import is_item_collected
from app.models import CollectionEntry, Item, MediaTypes, Sources


class CollectionListViewTest(TestCase):
    """Test collection list view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_collection_list_authenticated(self):
        """Test authenticated user can view their collection."""
        self.client.login(**self.credentials)
        response = self.client.get(reverse("collection_list"))
        self.assertEqual(response.status_code, 200)

    def test_collection_list_unauthenticated(self):
        """Test unauthenticated user is redirected to login."""
        response = self.client.get(reverse("collection_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_collection_list_filtered_by_media_type(self):
        """Test filtering by media_type parameter."""
        self.client.login(**self.credentials)

        # Create entries for different media types
        movie_item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="http://example.com/movie.jpg",
        )
        tv_item = Item.objects.create(
            media_id="tv1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
            image="http://example.com/tv.jpg",
        )

        CollectionEntry.objects.create(user=self.user, item=movie_item)
        CollectionEntry.objects.create(user=self.user, item=tv_item)

        # Filter by movie
        response = self.client.get(
            reverse("collection_list_filtered", kwargs={"media_type": MediaTypes.MOVIE.value}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["collection_entries"]), 1)
        self.assertEqual(
            response.context["collection_entries"][0].item.media_type,
            MediaTypes.MOVIE.value,
        )

    def test_collection_list_empty(self):
        """Test empty collection display."""
        self.client.login(**self.credentials)
        response = self.client.get(reverse("collection_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["collection_entries"]), 0)


class CollectionAddViewTest(TestCase):
    """Test collection add view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_collection_add_valid_data(self):
        """Test POST with valid data creates CollectionEntry."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_add"),
            {
                "item_id": self.item.id,
                "media_type": "bluray",
                "resolution": "1080p",
            },
        )

        # Should redirect or return success
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(CollectionEntry.objects.filter(user=self.user, item=self.item).exists())

    def test_collection_add_existing_entry_updates(self):
        """Test POST with existing entry updates instead of creating duplicate."""
        self.client.login(**self.credentials)

        # Create existing entry
        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="dvd",
        )

        # Try to add again with different data
        response = self.client.post(
            reverse("collection_add"),
            {
                "item_id": self.item.id,
                "media_type": "bluray",
                "resolution": "1080p",
            },
        )

        # Should update existing entry
        entry.refresh_from_db()
        self.assertEqual(entry.media_type, "bluray")
        self.assertEqual(entry.resolution, "1080p")

        # Should still only have one entry
        self.assertEqual(CollectionEntry.objects.filter(user=self.user, item=self.item).count(), 1)

    def test_collection_add_invalid_item_id(self):
        """Test validation errors for invalid item_id."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_add"),
            {
                "item_id": 99999,  # Non-existent ID
            },
        )

        # Should handle error gracefully
        self.assertIn(response.status_code, [400, 302])

    def test_collection_add_json_response(self):
        """Test JSON response for AJAX requests."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_add"),
            {
                "item_id": self.item.id,
                "media_type": "bluray",
            },
            HTTP_HX_REQUEST="true",
        )

        # Should return JSON
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["content-type"], "application/json")


class CollectionUpdateViewTest(TestCase):
    """Test collection update view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

        self.entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="dvd",
        )

    def test_collection_update_existing_entry(self):
        """Test POST updates existing CollectionEntry."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_update", kwargs={"entry_id": self.entry.id}),
            {
                "item": self.item.id,
                "media_type": "bluray",
                "resolution": "4k",
                "hdr": "HDR10",
            },
        )

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.media_type, "bluray")
        self.assertEqual(self.entry.resolution, "4k")
        self.assertEqual(self.entry.hdr, "HDR10")

    def test_collection_update_nonexistent_entry(self):
        """Test 404 for non-existent entry_id."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_update", kwargs={"entry_id": 99999}),
            {
                "item": self.item.id,
                "media_type": "bluray",
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_collection_update_other_user_entry(self):
        """Test user can only update their own entries."""
        self.client.login(**self.credentials)

        # Create another user and entry
        other_user = get_user_model().objects.create_user(
            username="other",
            password="12345",
        )
        other_entry = CollectionEntry.objects.create(
            user=other_user,
            item=self.item,
        )

        # Try to update other user's entry
        response = self.client.post(
            reverse("collection_update", kwargs={"entry_id": other_entry.id}),
            {
                "item": self.item.id,
                "media_type": "bluray",
            },
        )

        # Should return 404 (entry not found for this user)
        self.assertEqual(response.status_code, 404)


class CollectionRemoveViewTest(TestCase):
    """Test collection remove view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

        self.entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
        )

    def test_collection_remove_deletes_entry(self):
        """Test POST deletes CollectionEntry."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_remove", kwargs={"entry_id": self.entry.id}),
        )

        # Entry should be deleted
        self.assertFalse(CollectionEntry.objects.filter(id=self.entry.id).exists())

    def test_collection_remove_nonexistent_entry(self):
        """Test 404 for non-existent entry_id."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("collection_remove", kwargs={"entry_id": 99999}),
        )

        self.assertEqual(response.status_code, 404)

    def test_collection_remove_other_user_entry(self):
        """Test user can only delete their own entries."""
        self.client.login(**self.credentials)

        # Create another user and entry
        other_user = get_user_model().objects.create_user(
            username="other",
            password="12345",
        )
        other_entry = CollectionEntry.objects.create(
            user=other_user,
            item=self.item,
        )

        # Try to delete other user's entry
        response = self.client.post(
            reverse("collection_remove", kwargs={"entry_id": other_entry.id}),
        )

        # Should return 404 (entry not found for this user)
        self.assertEqual(response.status_code, 404)
        # Entry should still exist
        self.assertTrue(CollectionEntry.objects.filter(id=other_entry.id).exists())


class CollectionModalViewTest(TestCase):
    """Test collection modal view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_collection_modal_new_entry(self):
        """Test modal for new entry (no existing collection)."""
        self.client.login(**self.credentials)
        response = self.client.get(
            reverse(
                "collection_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "1234",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["entry"])

    def test_collection_modal_existing_entry(self):
        """Test modal for existing entry (pre-populated form)."""
        self.client.login(**self.credentials)

        entry = CollectionEntry.objects.create(
            user=self.user,
            item=self.item,
            media_type="bluray",
            resolution="1080p",
        )

        response = self.client.get(
            reverse(
                "collection_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "1234",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["entry"], entry)
        self.assertEqual(response.context["form"].instance, entry)
