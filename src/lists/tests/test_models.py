from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from app.models import CollectionEntry, Game, Item, MediaTypes, Movie, Sources, Status, TV
from lists.models import CustomList, CustomListItem


class CustomListModelTest(TestCase):
    """Test case for the CustomList model."""

    def setUp(self):
        """Set up test data for CustomList model."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )

        self.custom_list = CustomList.objects.create(
            name="Test List",
            description="Test Description",
            owner=self.user,
        )
        self.custom_list.collaborators.add(self.collaborator)

        self.item = Item.objects.create(
            title="Test Item",
            media_id="123",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
        )

        self.non_member_credentials = {
            "username": "non_member",
            "password": "12345",
        }
        self.non_member = get_user_model().objects.create_user(
            **self.non_member_credentials,
        )

    def test_custom_list_creation(self):
        """Test the creation of a CustomList instance."""
        self.assertEqual(self.custom_list.name, "Test List")
        self.assertEqual(self.custom_list.description, "Test Description")
        self.assertEqual(self.custom_list.owner, self.user)

    def test_custom_list_str_representation(self):
        """Test the string representation of a CustomList."""
        self.assertEqual(str(self.custom_list), "Test List")

    def test_owner_permissions(self):
        """Test owner permissions on custom list."""
        self.assertTrue(self.custom_list.user_can_view(self.user))
        self.assertTrue(self.custom_list.user_can_edit(self.user))
        self.assertTrue(self.custom_list.user_can_delete(self.user))

    def test_collaborator_permissions(self):
        """Test collaborator permissions on custom list."""
        self.assertTrue(self.custom_list.user_can_view(self.collaborator))
        self.assertTrue(self.custom_list.user_can_edit(self.collaborator))
        self.assertFalse(self.custom_list.user_can_delete(self.collaborator))

    def test_non_member_permissions(self):
        """Test non-member permissions on custom list."""
        self.assertFalse(self.custom_list.user_can_view(self.non_member))
        self.assertFalse(self.custom_list.user_can_edit(self.non_member))
        self.assertFalse(self.custom_list.user_can_delete(self.non_member))

    def test_duplicate_item_constraint(self):
        """Test that an item cannot be added twice to the same list."""
        CustomListItem.objects.create(
            item=self.item,
            custom_list=self.custom_list,
        )

        with self.assertRaises(IntegrityError):
            CustomListItem.objects.create(
                item=self.item,
                custom_list=self.custom_list,
            )


class CustomListManagerTest(TestCase):
    """Test case for the CustomListManager."""

    def setUp(self):
        """Set up test data for CustomListManager tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.other_credentials = {"username": "other", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.other_user = get_user_model().objects.create_user(**self.other_credentials)
        self.list1 = CustomList.objects.create(name="List 1", owner=self.user)
        self.list2 = CustomList.objects.create(name="List 2", owner=self.other_user)
        self.list2.collaborators.add(self.user)

    def test_get_user_lists(self):
        """Test the get_user_lists method of CustomListManager."""
        user_lists = CustomList.objects.get_user_lists(self.user)
        self.assertEqual(user_lists.count(), 2)
        self.assertIn(self.list1, user_lists)
        self.assertIn(self.list2, user_lists)


    def test_smart_list_sync_items(self):
        """Smart list should sync matching items from saved filters."""
        item = Item.objects.create(
            title="Smart Movie",
            media_id="456",
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            image="https://example.com/movie.jpg",
        )
        Movie.objects.create(item=item, user=self.user, status=Status.COMPLETED.value)

        smart_list = CustomList.objects.create(
            name="Smart",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.MOVIE.value],
            smart_filters={"status": "all", "rating": "all", "collection": "all"},
        )

        smart_list.sync_smart_items()
        self.assertTrue(smart_list.items.filter(id=item.id).exists())

    def test_smart_list_collection_filter_uses_episode_collection_for_tv(self):
        """Collected TV rules should match when related episodes are collected."""
        tv_item = Item.objects.create(
            title="Collected Show",
            media_id="777",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/tv.jpg",
        )
        TV.objects.create(item=tv_item, user=self.user, status=Status.IN_PROGRESS.value)

        episode_item = Item.objects.create(
            title="Collected Show Episode",
            media_id="777",
            media_type=MediaTypes.EPISODE.value,
            source=Sources.TMDB.value,
            season_number=1,
            episode_number=1,
            image="https://example.com/episode.jpg",
        )
        CollectionEntry.objects.create(user=self.user, item=episode_item)

        smart_list = CustomList.objects.create(
            name="Collected Shows",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.TV.value],
            smart_filters={"collection": "collected"},
        )

        smart_list.sync_smart_items()
        self.assertTrue(smart_list.items.filter(id=tv_item.id).exists())

    def test_smart_list_language_filter(self):
        """Language filter should match item language metadata."""
        item = Item.objects.create(
            title="English Movie",
            media_id="900",
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            image="https://example.com/english.jpg",
            languages=["en"],
        )
        Movie.objects.create(item=item, user=self.user, status=Status.COMPLETED.value)

        smart_list = CustomList.objects.create(
            name="English Movies",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.MOVIE.value],
            smart_filters={"language": "en"},
        )
        smart_list.sync_smart_items()
        self.assertTrue(smart_list.items.filter(id=item.id).exists())

    def test_smart_list_platform_filter(self):
        """Platform filter should match game platform metadata."""
        item = Item.objects.create(
            title="Switch Game",
            media_id="1200",
            media_type=MediaTypes.GAME.value,
            source=Sources.IGDB.value,
            image="https://example.com/game.jpg",
            platforms=["Switch"],
        )
        Game.objects.create(item=item, user=self.user, status=Status.COMPLETED.value)

        smart_list = CustomList.objects.create(
            name="Switch Games",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.GAME.value],
            smart_filters={"platform": "Switch"},
        )
        smart_list.sync_smart_items()
        self.assertTrue(smart_list.items.filter(id=item.id).exists())

    def test_smart_list_updates_on_media_status_and_delete(self):
        """Media save/delete events should incrementally add/remove smart memberships."""
        smart_list = CustomList.objects.create(
            name="Planning Games",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.GAME.value],
            smart_filters={"status": Status.PLANNING.value},
        )
        item = Item.objects.create(
            title="Signal Game",
            media_id="1300",
            media_type=MediaTypes.GAME.value,
            source=Sources.IGDB.value,
            image="https://example.com/signal-game.jpg",
        )

        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        self.assertTrue(smart_list.items.filter(id=item.id).exists())

        game.status = Status.COMPLETED.value
        game.save(update_fields=["status"])
        self.assertFalse(smart_list.items.filter(id=item.id).exists())

        game.status = Status.PLANNING.value
        game.save(update_fields=["status"])
        self.assertTrue(smart_list.items.filter(id=item.id).exists())

        game.delete()
        self.assertFalse(smart_list.items.filter(id=item.id).exists())

    def test_smart_list_updates_on_episode_collection_changes(self):
        """Episode collection ownership should incrementally update TV collection lists."""
        smart_list = CustomList.objects.create(
            name="Collected Shows",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.TV.value],
            smart_filters={"collection": "collected"},
        )
        tv_item = Item.objects.create(
            title="Collection Show",
            media_id="1400",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/collection-show.jpg",
        )
        TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            title="Collection Show Episode",
            media_id="1400",
            media_type=MediaTypes.EPISODE.value,
            source=Sources.TMDB.value,
            season_number=1,
            episode_number=1,
            image="https://example.com/collection-show-episode.jpg",
        )

        self.assertFalse(smart_list.items.filter(id=tv_item.id).exists())

        entry = CollectionEntry.objects.create(user=self.user, item=episode_item)
        self.assertTrue(smart_list.items.filter(id=tv_item.id).exists())

        entry.delete()
        self.assertFalse(smart_list.items.filter(id=tv_item.id).exists())
