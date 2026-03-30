import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Item, ItemTag, MediaTypes, Movie, Sources, Status, Tag


class TagsModalViewTest(TestCase):
    """Test the tags_modal view."""

    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="278",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Shawshank Redemption",
            image="http://example.com/image.jpg",
        )
        self.tag1 = Tag.objects.create(user=self.user, name="Favorite")
        self.tag2 = Tag.objects.create(user=self.user, name="Must Watch")

    def test_tags_modal_shows_user_tags(self):
        """Modal renders all user tags."""
        url = reverse(
            "tags_modal",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "media_id": "278",
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Favorite")
        self.assertContains(response, "Must Watch")

    def test_tags_modal_shows_applied_status(self):
        """Modal shows correct has_tag status."""
        ItemTag.objects.create(tag=self.tag1, item=self.item)
        url = reverse(
            "tags_modal",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "media_id": "278",
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # tag1 is applied, so its button should show "Remove"
        self.assertContains(response, "Remove")


class TagItemToggleViewTest(TestCase):
    """Test the tag_item_toggle view."""

    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="278",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Shawshank Redemption",
            image="http://example.com/image.jpg",
            genres=["Drama"],
        )
        self.tag = Tag.objects.create(user=self.user, name="Favorite")

    def test_add_tag_to_item(self):
        """Toggle adds tag when not present."""
        url = reverse("tag_item_toggle")
        response = self.client.post(
            url, {"tag_id": self.tag.id, "item_id": self.item.id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ItemTag.objects.filter(tag=self.tag, item=self.item).exists()
        )

    def test_toggle_returns_oob_preview_refresh(self):
        """Toggle response refreshes the detail-tag preview via OOB swap."""
        url = reverse("tag_item_toggle")
        response = self.client.post(
            url,
            {
                "tag_id": self.tag.id,
                "item_id": self.item.id,
                "preview_genres_json": json.dumps(["Drama"]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'hx-swap-oob="outerHTML"')
        self.assertContains(response, 'id="tag-preview-movie-278"')
        self.assertContains(response, 'data-has-preview="true"')
        self.assertContains(response, "Genres")
        self.assertContains(response, "Drama")
        self.assertContains(response, "Tags")
        self.assertContains(response, "Favorite")

    def test_remove_tag_from_item(self):
        """Toggle removes tag when already present."""
        ItemTag.objects.create(tag=self.tag, item=self.item)
        url = reverse("tag_item_toggle")
        response = self.client.post(
            url, {"tag_id": self.tag.id, "item_id": self.item.id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ItemTag.objects.filter(tag=self.tag, item=self.item).exists()
        )

    def test_cannot_toggle_other_user_tag(self):
        """Cannot toggle a tag owned by another user."""
        other_user = get_user_model().objects.create_user(
            username="other", password="12345"
        )
        other_tag = Tag.objects.create(user=other_user, name="Other Tag")
        url = reverse("tag_item_toggle")
        response = self.client.post(
            url, {"tag_id": other_tag.id, "item_id": self.item.id}
        )
        self.assertEqual(response.status_code, 404)


class TagCreateViewTest(TestCase):
    """Test the tag_create view."""

    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="278",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Shawshank Redemption",
            image="http://example.com/image.jpg",
            genres=["Drama"],
        )

    def test_create_tag(self):
        """Creates a new tag for the user."""
        url = reverse("tag_create")
        response = self.client.post(
            url, {"name": "New Tag", "item_id": self.item.id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Tag.objects.filter(user=self.user, name="New Tag").exists()
        )

    def test_create_returns_oob_preview_refresh(self):
        """Create response refreshes the detail-tag preview via OOB swap."""
        url = reverse("tag_create")
        response = self.client.post(
            url,
            {
                "name": "New Tag",
                "item_id": self.item.id,
                "preview_genres_json": json.dumps(["Drama"]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'hx-swap-oob="outerHTML"')
        self.assertContains(response, 'id="tag-preview-movie-278"')
        self.assertContains(response, "Genres")
        self.assertContains(response, "Drama")
        self.assertContains(response, "Tags")
        self.assertContains(response, "New Tag")

    def test_create_tag_auto_applies(self):
        """Tag is auto-applied to item when item_id provided."""
        url = reverse("tag_create")
        self.client.post(url, {"name": "New Tag", "item_id": self.item.id})
        tag = Tag.objects.get(user=self.user, name="New Tag")
        self.assertTrue(ItemTag.objects.filter(tag=tag, item=self.item).exists())

    def test_reject_duplicate_case_insensitive(self):
        """Rejects creating a tag with the same name (case-insensitive)."""
        Tag.objects.create(user=self.user, name="Favorite")
        url = reverse("tag_create")
        self.client.post(url, {"name": "favorite", "item_id": self.item.id})
        self.assertEqual(Tag.objects.filter(user=self.user).count(), 1)

    def test_reject_empty_name(self):
        """Rejects creating a tag with empty name."""
        url = reverse("tag_create")
        response = self.client.post(url, {"name": "", "item_id": self.item.id})
        self.assertEqual(response.status_code, 400)


class TagFilterViewTest(TestCase):
    """Test tag filtering in the media_list view."""

    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item1 = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Tagged Movie",
            image="http://example.com/image.jpg",
        )
        self.item2 = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Untagged Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=self.item1,
            user=self.user,
            status=Status.COMPLETED,
        )
        Movie.objects.create(
            item=self.item2,
            user=self.user,
            status=Status.COMPLETED,
        )

        self.tag = Tag.objects.create(user=self.user, name="Favorite")
        ItemTag.objects.create(tag=self.tag, item=self.item1)

    def test_include_tag_filter(self):
        """Tag filter shows only items with the tag."""
        url = reverse("medialist", args=["movie"])
        response = self.client.get(url, {"tag": "Favorite"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tagged Movie")
        self.assertNotContains(response, "Untagged Movie")

    def test_exclude_tag_filter(self):
        """Tag exclude filter hides items with the tag."""
        url = reverse("medialist", args=["movie"])
        response = self.client.get(url, {"tag_exclude": "Favorite"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Tagged Movie")
        self.assertContains(response, "Untagged Movie")

    def test_tag_filter_case_insensitive(self):
        """Tag filter is case-insensitive."""
        url = reverse("medialist", args=["movie"])
        response = self.client.get(url, {"tag": "favorite"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tagged Movie")
        self.assertNotContains(response, "Untagged Movie")

    def test_no_tag_filter_shows_all(self):
        """No tag filter shows all items."""
        url = reverse("medialist", args=["movie"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tagged Movie")
        self.assertContains(response, "Untagged Movie")
