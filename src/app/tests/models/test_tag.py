from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from app.models import Item, ItemTag, MediaTypes, Sources, Tag


class TagModelTest(TestCase):
    """Test Tag model creation, constraints, and normalization."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="testuser", password="12345"
        )
        self.user2 = get_user_model().objects.create_user(
            username="testuser2", password="12345"
        )

    def test_create_tag(self):
        tag = Tag.objects.create(user=self.user, name="Favorite")
        self.assertEqual(str(tag), "Favorite")
        self.assertEqual(tag.user, self.user)

    def test_case_insensitive_uniqueness(self):
        """Same user cannot create tags differing only by case."""
        Tag.objects.create(user=self.user, name="Favorite")
        with self.assertRaises(IntegrityError):
            Tag.objects.create(user=self.user, name="favorite")

    def test_different_users_same_name(self):
        """Different users can have identically named tags."""
        Tag.objects.create(user=self.user, name="Favorite")
        tag2 = Tag.objects.create(user=self.user2, name="Favorite")
        self.assertEqual(tag2.name, "Favorite")

    def test_name_whitespace_normalization(self):
        """Whitespace is stripped and internal spaces collapsed."""
        tag = Tag.objects.create(user=self.user, name="  comfort   show  ")
        self.assertEqual(tag.name, "comfort show")

    def test_ordering(self):
        """Tags are ordered alphabetically by name."""
        Tag.objects.create(user=self.user, name="Zebra")
        Tag.objects.create(user=self.user, name="Alpha")
        Tag.objects.create(user=self.user, name="Middle")
        names = list(Tag.objects.filter(user=self.user).values_list("name", flat=True))
        self.assertEqual(names, ["Alpha", "Middle", "Zebra"])

    def test_cascade_delete_user(self):
        """Deleting user cascades to their tags."""
        Tag.objects.create(user=self.user, name="Temp")
        self.user.delete()
        self.assertEqual(Tag.objects.count(), 0)


class ItemTagModelTest(TestCase):
    """Test ItemTag join model and constraints."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="testuser", password="12345"
        )
        self.item = Item.objects.create(
            media_id="movie1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/movie.jpg",
        )
        self.tag = Tag.objects.create(user=self.user, name="Favorite")

    def test_create_item_tag(self):
        item_tag = ItemTag.objects.create(tag=self.tag, item=self.item)
        self.assertEqual(str(item_tag), "Favorite -> Test Movie")

    def test_unique_tag_item(self):
        """Cannot apply the same tag to the same item twice."""
        ItemTag.objects.create(tag=self.tag, item=self.item)
        with self.assertRaises(IntegrityError):
            ItemTag.objects.create(tag=self.tag, item=self.item)

    def test_cascade_delete_tag(self):
        """Deleting tag cascades to its ItemTag rows."""
        ItemTag.objects.create(tag=self.tag, item=self.item)
        self.tag.delete()
        self.assertEqual(ItemTag.objects.count(), 0)

    def test_cascade_delete_item(self):
        """Deleting item cascades to its ItemTag rows."""
        ItemTag.objects.create(tag=self.tag, item=self.item)
        self.item.delete()
        self.assertEqual(ItemTag.objects.count(), 0)

    def test_multiple_tags_on_item(self):
        """An item can have multiple tags from the same user."""
        tag2 = Tag.objects.create(user=self.user, name="Must Watch")
        ItemTag.objects.create(tag=self.tag, item=self.item)
        ItemTag.objects.create(tag=tag2, item=self.item)
        self.assertEqual(ItemTag.objects.filter(item=self.item).count(), 2)
