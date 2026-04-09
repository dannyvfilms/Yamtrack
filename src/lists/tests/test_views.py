import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import TV, Anime, Episode, Item, MediaTypes, Movie, Season, Sources, Status
from lists.models import CustomList, CustomListItem, ListActivity
from users.models import DateFormatChoices


class ListsViewTests(TestCase):
    """Tests for the lists view."""

    def setUp(self):
        """Set up test data for lists view tests."""
        self.factory = RequestFactory()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )

        # Create some test lists
        self.list1 = CustomList.objects.create(
            name="Test List 1",
            description="Description 1",
            owner=self.user,
        )
        self.list2 = CustomList.objects.create(
            name="Test List 2",
            description="Description 2",
            owner=self.user,
        )

        # Add collaborator to one list
        self.list1.collaborators.add(self.collaborator)

        # Create some items
        self.item1 = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
        )
        self.item2 = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )

        # Add items to lists
        CustomListItem.objects.create(
            custom_list=self.list1,
            item=self.item1,
        )
        CustomListItem.objects.create(
            custom_list=self.list2,
            item=self.item2,
        )

    def test_lists_owner_view(self):
        """Test the lists view response and context for owner."""
        self.client.login(**self.credentials)
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/custom_lists.html")
        self.assertIn("custom_lists", response.context)
        self.assertIn("form", response.context)

    def test_lists_collaborator_view(self):
        """Test the lists view response and context for a collaborator."""
        self.client.login(**self.collaborator_credentials)
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/custom_lists.html")
        self.assertIn("custom_lists", response.context)
        self.assertIn("form", response.context)

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_search_filter(self, mock_update_preference):
        """Test the lists view with search filter."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        # Test search by name
        response = self.client.get(reverse("lists") + "?q=List 1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 1)
        self.assertEqual(response.context["custom_lists"][0].name, "Test List 1")

        # Test search by description
        response = self.client.get(reverse("lists") + "?q=Description 2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 1)
        self.assertEqual(response.context["custom_lists"][0].name, "Test List 2")

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_sorting(self, mock_update_preference):
        """Test the lists view with different sorting options."""
        self.client.login(**self.credentials)

        # Test name sorting
        mock_update_preference.return_value = "name"
        response = self.client.get(reverse("lists") + "?sort=name")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "name")
        self.assertEqual(response.context["current_direction"], "asc")

        # Test items_count sorting
        mock_update_preference.return_value = "items_count"
        response = self.client.get(reverse("lists") + "?sort=items_count")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "items_count")
        self.assertEqual(response.context["current_direction"], "desc")

        # Test newest_first sorting
        mock_update_preference.return_value = "newest_first"
        response = self.client.get(reverse("lists") + "?sort=newest_first")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "newest_first")
        self.assertEqual(response.context["current_direction"], "desc")

        # Test last_watched sorting
        mock_update_preference.return_value = "last_watched"
        response = self.client.get(reverse("lists") + "?sort=last_watched")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "last_watched")
        self.assertEqual(response.context["current_direction"], "desc")

        # Test default sorting (last_item_added)
        mock_update_preference.return_value = "last_item_added"
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "last_item_added")
        self.assertEqual(response.context["current_direction"], "desc")

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_name_sort_honors_direction(self, mock_update_preference):
        """Name sorting should flip ordering when direction changes."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        asc_response = self.client.get(reverse("lists") + "?sort=name")
        self.assertEqual(asc_response.status_code, 200)
        self.assertEqual(asc_response.context["current_direction"], "asc")
        self.assertEqual(
            [custom_list.name for custom_list in asc_response.context["custom_lists"]],
            ["Test List 1", "Test List 2"],
        )

        desc_response = self.client.get(reverse("lists") + "?sort=name&direction=desc")
        self.assertEqual(desc_response.status_code, 200)
        self.assertEqual(desc_response.context["current_direction"], "desc")
        self.assertEqual(
            [custom_list.name for custom_list in desc_response.context["custom_lists"]],
            ["Test List 2", "Test List 1"],
        )

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_last_watched_sort_orders_by_latest_content_watch(
        self,
        mock_update_preference,
    ):
        """Hub sorting should use the latest watched date across each list's contents."""
        mock_update_preference.return_value = "last_watched"
        self.client.login(**self.credentials)
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])

        older_watch = datetime(2026, 4, 5, 18, 0, tzinfo=UTC)
        newer_watch = datetime(2026, 4, 8, 18, 0, tzinfo=UTC)

        Movie.objects.bulk_create(
            [
                Movie(
                    item=self.item1,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    end_date=older_watch,
                ),
            ],
        )

        tv = TV.objects.create(
            item=self.item2,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            season_number=1,
            image="http://example.com/season.jpg",
        )
        episode_item = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Pilot",
            season_number=1,
            episode_number=1,
            image="http://example.com/episode.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        Episode.objects.bulk_create(
            [
                Episode(
                    item=episode_item,
                    related_season=season,
                    end_date=newer_watch,
                ),
            ],
        )

        response = self.client.get(reverse("lists") + "?sort=last_watched")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_direction"], "desc")
        self.assertEqual(
            [custom_list.name for custom_list in response.context["custom_lists"]],
            ["Test List 2", "Test List 1"],
        )
        self.assertContains(response, timezone.localtime(newer_watch).strftime("%Y-%m-%d"))
        self.assertContains(response, timezone.localtime(older_watch).strftime("%Y-%m-%d"))

        asc_response = self.client.get(reverse("lists") + "?sort=last_watched&direction=asc")

        self.assertEqual(asc_response.status_code, 200)
        self.assertEqual(asc_response.context["current_direction"], "asc")
        self.assertEqual(
            [custom_list.name for custom_list in asc_response.context["custom_lists"]],
            ["Test List 1", "Test List 2"],
        )

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_last_watched_htmx_request_shows_helper_dates(
        self,
        mock_update_preference,
    ):
        """HTMX list-grid refresh should render the last-watched helper row."""
        mock_update_preference.return_value = "last_watched"
        self.client.login(**self.credentials)
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])

        watched_at = datetime(2026, 4, 8, 18, 0, tzinfo=UTC)

        Movie.objects.bulk_create(
            [
                Movie(
                    item=self.item1,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    end_date=watched_at,
                ),
            ],
        )

        response = self.client.get(
            reverse("lists") + "?sort=last_watched",
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_grid.html")
        self.assertEqual(response.context["current_sort"], "last_watched")
        self.assertEqual(response.context["current_direction"], "desc")
        self.assertContains(response, timezone.localtime(watched_at).strftime("%Y-%m-%d"))
        self.assertContains(response, "No watched items")

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_htmx_request(self, mock_update_preference):
        """Test the lists view with HTMX request."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        # Make an HTMX request
        response = self.client.get(reverse("lists"), headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_grid.html")

        self.assertIn("custom_lists", response.context)
        self.assertEqual(response.context["current_direction"], "asc")

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_pagination(self, mock_update_preference):
        """Test the lists view pagination."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        # Create more lists to test pagination
        for i in range(25):  # Create 25 more lists (27 total)
            CustomList.objects.create(
                name=f"Paginated List {i}",
                owner=self.user,
            )

        # Test first page
        response = self.client.get(reverse("lists"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 20)  # 20 per page

        # Test second page
        response = self.client.get(reverse("lists") + "?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["custom_lists"]), 7)  # 7 remaining items

    @patch.object(get_user_model(), "update_preference")
    def test_lists_view_keeps_card_actions_as_edit_buttons(
        self,
        mock_update_preference,
    ):
        """The list overview should keep card controls as edit buttons only."""
        mock_update_preference.return_value = "name"
        self.client.login(**self.credentials)

        smart_list = CustomList.objects.create(
            name="Smart List",
            description="Automatic",
            owner=self.user,
            is_smart=True,
        )

        response = self.client.get(reverse("lists"))

        self.assertContains(response, 'title="Edit list"')
        self.assertNotContains(response, reverse("list_add_item", args=[self.list1.id]))
        self.assertNotContains(
            response,
            f'{reverse("list_detail", args=[smart_list.id])}?edit_smart_rules=1',
        )
        self.assertContains(response, "More list actions")


class ListDetailViewTests(TestCase):
    """Tests for the list_detail view."""

    def setUp(self):
        """Set up test data."""
        self.factory = RequestFactory()
        self.credentials = {"username": "testuser", "password": "testpassword"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.other_credentials = {
            "username": "otheruser",
            "password": "testpassword",
        }
        self.other_user = get_user_model().objects.create_user(
            **self.other_credentials,
        )
        self.client.login(**self.credentials)

        # Create a test list
        self.custom_list = CustomList.objects.create(
            name="Test List",
            description="Test Description",
            owner=self.user,
        )

        # Create some items with different media types
        self.movie_item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
        )
        self.tv_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )
        self.anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
        )

        # Add items to the list
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.movie_item,
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.tv_item,
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.anime_item,
        )

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create Movie instance
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        # Create TV instance
        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        # Create Anime instance
        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view
        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/list_detail.html")

        # Check context data
        self.assertEqual(response.context["custom_list"], self.custom_list)
        self.assertEqual(len(response.context["items"]), 3)
        self.assertEqual(response.context["current_sort"], "date_added")
        self.assertEqual(response.context["items_count"], 3)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_end_date_label_remains_end_date(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """List-detail end_date sort should still be labeled End Date."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dict(response.context["sort_choices"])["end_date"], "End Date")

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_unauthorized(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view when user is not authorized."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = False

        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))
        self.assertEqual(response.status_code, 404)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_filter_by_media_type(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with media type filter."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view with media type filter
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id])
            + f"?type={MediaTypes.MOVIE.value}",
        )
        self.assertEqual(response.status_code, 200)

        # Should only have the movie item
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(
            response.context["items"][0].media_type,
            MediaTypes.MOVIE.value,
        )

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_filter_by_status(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with status filter."""
        mock_update_preference.side_effect = ["date_added", Status.PLANNING.value]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view with status filter
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id])
            + f"?status={Status.PLANNING.value}",
        )
        self.assertEqual(response.status_code, 200)

        # Check that filters are applied
        self.assertEqual(
            response.context["current_status"],
            Status.PLANNING.value,
        )
        # Should only have the PLANNING item of media type ANIME
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(
            response.context["items"][0].media_type,
            MediaTypes.ANIME.value,
        )

    def test_list_detail_view_anonymous_public(self):
        """Ensure anonymous users can view public lists without preference errors."""
        self.custom_list.visibility = "public"
        self.custom_list.save(update_fields=["visibility"])

        self.client.logout()
        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))
        self.assertEqual(response.status_code, 200)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_search(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with search filter."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test the view with search filter
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?q=Anime",
        )
        self.assertEqual(response.status_code, 200)

        # Should only have the anime item
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(response.context["items"][0].title, "Test Anime")

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_sorting(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with different sorting options."""
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Test title sorting
        mock_update_preference.side_effect = ["title", None]
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=title",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")

        # Test media_type sorting
        mock_update_preference.side_effect = ["media_type", None]
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=media_type",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "media_type")

        # Test rating sorting
        mock_update_preference.side_effect = None
        mock_update_preference.return_value = "rating"
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=rating",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "rating")

        # Test progress sorting
        mock_update_preference.return_value = "progress"
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=progress",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "progress")

        # Test start_date sorting
        mock_update_preference.return_value = "start_date"
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=start_date",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "start_date")

        # Test end_date sorting
        mock_update_preference.return_value = "end_date"
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=end_date",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "end_date")

        # Test release_date sorting
        mock_update_preference.return_value = "release_date"
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=release_date",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "release_date")

        # Test custom sorting
        mock_update_preference.return_value = "custom"
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=custom",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "custom")

    def test_release_date_sort_orders_items_and_renders_subtitles(self):
        """Release-date sort should persist, order items, and render full dates."""
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])

        self.tv_item.release_datetime = datetime(2019, 3, 10, 12, 0, 0, tzinfo=UTC)
        self.movie_item.release_datetime = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.anime_item.release_datetime = datetime(2021, 7, 15, 12, 0, 0, tzinfo=UTC)
        self.tv_item.save(update_fields=["release_datetime"])
        self.movie_item.save(update_fields=["release_datetime"])
        self.anime_item.save(update_fields=["release_datetime"])

        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=release_date",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "release_date")

        ordered_titles = [item.title for item in response.context["items"]]
        self.assertEqual(ordered_titles, ["Test TV Show", "Test Movie", "Test Anime"])

        self.user.refresh_from_db()
        self.assertEqual(self.user.list_detail_sort, "release_date")

        self.assertContains(response, "2019-03-10")
        self.assertContains(response, "2020-01-01")
        self.assertContains(response, "2021-07-15")

    def test_release_date_sort_honors_direction(self):
        """Release-date sort should reverse ordering when direction switches."""
        self.tv_item.release_datetime = datetime(2019, 3, 10, 12, 0, 0, tzinfo=UTC)
        self.movie_item.release_datetime = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.anime_item.release_datetime = datetime(2021, 7, 15, 12, 0, 0, tzinfo=UTC)
        self.tv_item.save(update_fields=["release_datetime"])
        self.movie_item.save(update_fields=["release_datetime"])
        self.anime_item.save(update_fields=["release_datetime"])

        asc_response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=release_date&direction=asc",
        )
        self.assertEqual(asc_response.status_code, 200)
        self.assertEqual(asc_response.context["current_direction"], "asc")
        self.assertEqual(
            [item.title for item in asc_response.context["items"]],
            ["Test TV Show", "Test Movie", "Test Anime"],
        )

        desc_response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=release_date&direction=desc",
        )
        self.assertEqual(desc_response.status_code, 200)
        self.assertEqual(desc_response.context["current_direction"], "desc")
        self.assertEqual(
            [item.title for item in desc_response.context["items"]],
            ["Test Anime", "Test Movie", "Test TV Show"],
        )

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    @patch("app.providers.services.get_media_metadata")
    def test_list_detail_view_rating_sorting(
        self,
        mock_get_media_metadata,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with rating sorting."""
        mock_user_can_view.return_value = True
        mock_update_preference.return_value = "rating"

        # Mock the media metadata to avoid API calls
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Media",
        }

        # Create model instances with different ratings
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
            score=8.5,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
            score=9.0,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
            score=7.5,
        )

        # Test rating sorting - should be in descending order (highest first)
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?sort=rating",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "rating")

        # Check that items are sorted by rating (highest first)
        items = response.context["items"]
        self.assertEqual(len(items), 3)
        # First item should have highest rating (9.0)
        self.assertEqual(items[0].media.score, 9.0)
        # Second item should have second highest rating (8.5)
        self.assertEqual(items[1].media.score, 8.5)
        # Third item should have lowest rating (7.5)
        self.assertEqual(items[2].media.score, 7.5)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_view_htmx_request(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Test the list_detail view with HTMX request."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        # Create model instances
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        Anime.objects.create(
            item=self.anime_item,
            status=Status.PLANNING.value,
            user=self.user,
        )

        # Make an HTMX request
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]),
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/media_grid.html")
        self.assertNotIn("form", response.context)

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    @patch("app.providers.services.get_media_metadata")
    def test_list_detail_view_table_layout_full_render(
        self,
        mock_get_media_metadata,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Full list detail renders the table layout when requested."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Movie",
        }

        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?layout=table",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_layout"], "table")
        self.assertEqual(
            [column.key for column in response.context["resolved_columns"]],
            [
                "image",
                "title",
                "media_type",
                "score",
                "progress",
                "status",
                "release_date",
                "date_added",
                "start_date",
                "end_date",
            ],
        )
        self.assertContains(response, 'id="list-table-body"')
        self.assertContains(response, 'min-w-10 w-10 h-10 object-cover rounded-md')
        self.assertContains(response, 'id="media-column-config-data"')

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    @patch("app.providers.services.get_media_metadata")
    def test_list_detail_view_table_partial(
        self,
        mock_get_media_metadata,
        mock_user_can_view,
        mock_update_preference,
    ):
        """HTMX table layout requests should return the list-table partial."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Movie",
        }

        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]) + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_table.html")
        self.assertContains(response, 'class="w-full bg-[#2a2f35] media-table"')

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_table_column_preferences_are_scoped_to_lists(self, mock_user_can_view, mock_update_preference):
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        self.client.post(
            reverse("list_detail_columns", args=[self.custom_list.id]),
            {
                "table_type": "list",
                "media_type_key": MediaTypes.MOVIE.value,
                "sort": "rating",
                "order": json.dumps(["media_type", "status"]),
                "hidden": json.dumps(["status"]),
            },
            headers={"hx-request": "true"},
        )

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.MOVIE.value]["list"],
            {
                "order": [
                    "media_type",
                    "status",
                    "score",
                    "runtime",
                    "time_watched",
                    "popularity",
                    "release_date",
                    "date_added",
                    "start_date",
                    "end_date",
                ],
                "hidden": ["status"],
            },
        )

    def test_smart_list_detail_uses_smart_template(self):
        """Smart lists should render the dedicated smart detail view."""
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )
        smart_list = CustomList.objects.create(
            name="Smart List",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.MOVIE.value],
            smart_filters={"status": "all"},
        )

        response = self.client.get(reverse("list_detail", args=[smart_list.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/smart_list_detail.html")
        self.assertTrue(response.context["is_smart_list"])

    def test_manual_list_detail_exposes_quick_add_split_actions(self):
        """Editable manual lists should use quick-add actions in the detail header."""
        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("list_add_item", args=[self.custom_list.id]))
        self.assertContains(response, "Add New Item")
        self.assertContains(response, "More list actions")
        self.assertNotContains(response, 'aria-label="Edit list"')

    @patch("app.providers.services.get_media_metadata")
    def test_smart_list_detail_exposes_smart_rule_split_actions(
        self,
        mock_get_media_metadata,
    ):
        """Editable smart lists should use the smart-rule split button in the header."""
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Movie",
        }
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )
        smart_list = CustomList.objects.create(
            name="Smart List",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.MOVIE.value],
            smart_filters={"status": "all"},
        )

        response = self.client.get(reverse("list_detail", args=[smart_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'{reverse("list_detail", args=[smart_list.id])}?edit_smart_rules=1',
        )
        self.assertContains(response, "More list actions")
        self.assertNotContains(response, 'aria-label="Edit list metadata"')

    @patch("app.providers.services.get_media_metadata")
    def test_smart_list_detail_table_partial(self, mock_get_media_metadata):
        """Smart list table layout should return list-table partials for HTMX."""
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Movie",
        }
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )
        smart_list = CustomList.objects.create(
            name="Smart List",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.MOVIE.value],
            smart_filters={"status": "all"},
        )

        response = self.client.get(
            reverse("list_detail", args=[smart_list.id]) + "?edit_smart_rules=1&layout=table",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_table.html")

    @patch("app.providers.services.get_media_metadata")
    def test_public_smart_list_filters_without_persisting_rules(
        self,
        mock_get_media_metadata,
    ):
        """Public smart-list filtering should not mutate saved rules."""
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Media",
        }
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )
        TV.objects.create(
            item=self.tv_item,
            status=Status.IN_PROGRESS.value,
            user=self.user,
        )

        smart_list = CustomList.objects.create(
            name="Public Smart List",
            owner=self.user,
            is_smart=True,
            visibility="public",
            smart_media_types=[MediaTypes.MOVIE.value, MediaTypes.TV.value],
            smart_filters={"status": "all"},
        )

        self.client.logout()

        response = self.client.get(reverse("list_detail", args=[smart_list.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="smart-filter-form"')
        self.assertEqual(len(response.context["items"]), 2)

        filtered_response = self.client.get(
            reverse("list_detail", args=[smart_list.id])
            + f"?status={Status.COMPLETED.value}",
        )
        self.assertEqual(filtered_response.status_code, 200)
        self.assertFalse(filtered_response.context["smart_edit_mode"])
        self.assertEqual(
            filtered_response.context["active_smart_rules"]["status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(
            filtered_response.context["saved_smart_rules"]["status"],
            "all",
        )
        self.assertEqual(len(filtered_response.context["items"]), 1)
        self.assertEqual(
            filtered_response.context["items"][0].id,
            self.movie_item.id,
        )

        smart_list.refresh_from_db()
        self.assertEqual(smart_list.smart_filters.get("status"), "all")

    @patch("app.providers.services.get_media_metadata")
    def test_public_smart_list_anonymous_view_preserves_saved_media_types(
        self,
        mock_get_media_metadata,
    ):
        """Anonymous public smart-list loads must honor saved media-type rules."""
        mock_get_media_metadata.return_value = {
            "max_progress": 1,
            "related": {"seasons": []},
            "title": "Test Media",
        }
        Movie.objects.create(
            item=self.movie_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )
        TV.objects.create(
            item=self.tv_item,
            status=Status.COMPLETED.value,
            user=self.user,
        )

        smart_list = CustomList.objects.create(
            name="Public Movies",
            owner=self.user,
            is_smart=True,
            visibility="public",
            smart_media_types=[MediaTypes.MOVIE.value],
            smart_filters={"status": "all"},
        )

        self.client.logout()

        response = self.client.get(reverse("list_detail", args=[smart_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["saved_smart_rules"]["media_types"],
            [MediaTypes.MOVIE.value],
        )
        self.assertEqual(
            response.context["active_smart_rules"]["media_types"],
            [MediaTypes.MOVIE.value],
        )
        self.assertEqual(
            [item.id for item in response.context["items"]],
            [self.movie_item.id],
        )

        filtered_response = self.client.get(
            reverse("list_detail", args=[smart_list.id])
            + f"?status={Status.COMPLETED.value}",
        )

        self.assertEqual(filtered_response.status_code, 200)
        self.assertEqual(
            filtered_response.context["active_smart_rules"]["media_types"],
            [MediaTypes.MOVIE.value],
        )
        self.assertEqual(
            [item.id for item in filtered_response.context["items"]],
            [self.movie_item.id],
        )

    def test_smart_list_release_date_sort_honors_direction(self):
        """Smart-list release-date sort should reverse ordering by direction."""
        self.movie_item.release_datetime = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.tv_item.release_datetime = datetime(2021, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.anime_item.release_datetime = datetime(2019, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.movie_item.save(update_fields=["release_datetime"])
        self.tv_item.save(update_fields=["release_datetime"])
        self.anime_item.save(update_fields=["release_datetime"])

        Movie.objects.create(item=self.movie_item, status=Status.COMPLETED.value, user=self.user)
        TV.objects.create(item=self.tv_item, status=Status.IN_PROGRESS.value, user=self.user)
        Anime.objects.create(item=self.anime_item, status=Status.PLANNING.value, user=self.user)

        smart_list = CustomList.objects.create(
            name="Smart List",
            owner=self.user,
            is_smart=True,
            smart_media_types=[
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.ANIME.value,
            ],
            smart_filters={"status": "all"},
        )

        asc_response = self.client.get(
            reverse("list_detail", args=[smart_list.id]) + "?sort=release_date&direction=asc",
        )
        self.assertEqual(asc_response.status_code, 200)
        self.assertEqual(asc_response.context["current_direction"], "asc")
        self.assertEqual(
            [item.title for item in asc_response.context["items"]],
            ["Test Anime", "Test Movie", "Test TV Show"],
        )

        desc_response = self.client.get(
            reverse("list_detail", args=[smart_list.id]) + "?sort=release_date&direction=desc",
        )
        self.assertEqual(desc_response.status_code, 200)
        self.assertEqual(desc_response.context["current_direction"], "desc")
        self.assertEqual(
            [item.title for item in desc_response.context["items"]],
            ["Test TV Show", "Test Movie", "Test Anime"],
        )


    @patch("app.providers.services.get_media_metadata")
    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_with_episode_rating_sort(
        self,
        mock_user_can_view,
        mock_update_preference,
        mock_get_metadata,
    ):
        """Episode items in lists must not cause 500 when sorted by rating (issue #93)."""
        mock_update_preference.return_value = "rating"
        mock_user_can_view.return_value = True
        # Make the API call fail gracefully so Episode.save() doesn't error
        mock_get_metadata.side_effect = KeyError("no api in tests")

        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Friends S1E1",
            season_number=1,
            episode_number=1,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
        )

        episode_list = CustomList.objects.create(
            name="Episode List",
            owner=self.user,
        )
        CustomListItem.objects.create(
            custom_list=episode_list,
            item=episode_item,
        )

        response = self.client.get(
            reverse("list_detail", args=[episode_list.id]) + "?sort=rating",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), 1)
        self.assertContains(response, "S01 E01")

    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_untracked_episode_cards_use_season_poster_and_visible_identity_subtitle(
        self,
        mock_user_can_view,
        mock_update_preference,
    ):
        """Episode list cards should still show season art and Sxx Exx without tracking rows."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True

        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            season_number=1,
            image="http://example.com/season.jpg",
        )
        episode_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="The One Where It Starts",
            season_number=1,
            episode_number=1,
            image=settings.IMG_NONE,
        )

        episode_list = CustomList.objects.create(
            name="Episode List",
            owner=self.user,
        )
        CustomListItem.objects.create(
            custom_list=episode_list,
            item=episode_item,
        )

        response = self.client.get(reverse("list_detail", args=[episode_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "S01 E01")
        self.assertContains(response, "http://example.com/season.jpg")
        self.assertContains(response, "media-card-subtitle-always")

    @patch("lists.views.services.get_media_metadata")
    @patch.object(get_user_model(), "update_preference")
    @patch.object(CustomList, "user_can_view")
    def test_list_detail_zero_season_episode_cards_show_identity_and_backfilled_episode_title(
        self,
        mock_user_can_view,
        mock_update_preference,
        mock_get_metadata,
    ):
        """Season 0 episode cards should still show S00 Exx and the episode title."""
        mock_update_preference.side_effect = ["date_added", None]
        mock_user_can_view.return_value = True
        mock_get_metadata.return_value = {
            "title": "Death Note",
            "season_title": "Specials",
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "Rebirth",
                    "image": settings.IMG_NONE,
                },
            ],
            "image": settings.IMG_NONE,
        }

        season_item = Item.objects.create(
            media_id="13916",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Death Note",
            season_number=0,
            image="http://example.com/season-zero.jpg",
        )
        episode_item = Item.objects.create(
            media_id="13916",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Death Note",
            season_number=0,
            episode_number=1,
            image=settings.IMG_NONE,
        )
        season_entry = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season_entry,
        )

        episode_list = CustomList.objects.create(
            name="Death Note Episodes",
            owner=self.user,
        )
        CustomListItem.objects.create(
            custom_list=episode_list,
            item=episode_item,
        )

        response = self.client.get(reverse("list_detail", args=[episode_list.id]))
        episode_item.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "S00 E01")
        self.assertContains(response, "Rebirth")
        self.assertEqual(episode_item.title, "Rebirth")
        self.assertContains(response, "http://example.com/season-zero.jpg")


class CreateListViewTest(TestCase):
    """Test case for the create list view."""

    def setUp(self):
        """Set up test data for create list view tests."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_create_list(self):
        """Test creating a new custom list."""
        self.client.post(
            reverse("list_create"),
            {"name": "New List", "description": "New Description"},
        )
        self.assertEqual(CustomList.objects.count(), 1)
        new_list = CustomList.objects.first()
        self.assertEqual(new_list.name, "New List")
        self.assertEqual(new_list.description, "New Description")
        self.assertEqual(new_list.owner, self.user)

    def test_create_smart_list_redirects_to_builder(self):
        """Smart-create flow should land on detail page in smart edit mode."""
        response = self.client.post(
            reverse("list_create"),
            {
                "name": "Smart List",
                "description": "",
                "is_smart": "on",
                "smart_create_flow": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        smart_list = CustomList.objects.get(name="Smart List")
        self.assertEqual(
            response.url,
            reverse("list_detail", args=[smart_list.id]) + "?edit_smart_rules=1",
        )


class SmartRulesUpdateViewTest(TestCase):
    """Tests for smart rules autosave endpoint."""

    def setUp(self):
        self.client = Client()
        self.owner = get_user_model().objects.create_user(
            username="owner",
            password="12345",
        )
        self.collaborator = get_user_model().objects.create_user(
            username="collab",
            password="12345",
        )
        self.outsider = get_user_model().objects.create_user(
            username="outsider",
            password="12345",
        )
        self.item = Item.objects.create(
            media_id="500",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Smart Match",
            image="https://example.com/smart.jpg",
        )
        Movie.objects.create(
            item=self.item,
            user=self.owner,
            status=Status.COMPLETED.value,
        )

        self.smart_list = CustomList.objects.create(
            name="Smart",
            owner=self.owner,
            is_smart=True,
        )
        self.smart_list.collaborators.add(self.collaborator)

        self.manual_list = CustomList.objects.create(
            name="Manual",
            owner=self.owner,
            is_smart=False,
        )

    def test_owner_can_update_smart_rules(self):
        self.client.login(username="owner", password="12345")
        response = self.client.post(
            reverse("list_smart_rules_update", args=[self.smart_list.id]),
            data=json.dumps(
                {
                    "media_types": [MediaTypes.MOVIE.value],
                    "status": "all",
                    "rating": "all",
                    "collection": "all",
                    "search": "Smart",
                },
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.smart_list.refresh_from_db()
        self.assertEqual(self.smart_list.smart_media_types, [MediaTypes.MOVIE.value])
        self.assertEqual(self.smart_list.smart_filters["search"], "Smart")
        self.assertTrue(self.smart_list.items.filter(id=self.item.id).exists())

    def test_collaborator_can_update_smart_rules(self):
        self.client.login(username="collab", password="12345")
        response = self.client.post(
            reverse("list_smart_rules_update", args=[self.smart_list.id]),
            data=json.dumps({"media_types": [MediaTypes.MOVIE.value]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def test_outsider_cannot_update_smart_rules(self):
        self.client.login(username="outsider", password="12345")
        response = self.client.post(
            reverse("list_smart_rules_update", args=[self.smart_list.id]),
            data=json.dumps({"media_types": [MediaTypes.MOVIE.value]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_manual_list_rejects_smart_rule_updates(self):
        self.client.login(username="owner", password="12345")
        response = self.client.post(
            reverse("list_smart_rules_update", args=[self.manual_list.id]),
            data=json.dumps({"media_types": [MediaTypes.MOVIE.value]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class EditListViewTest(TestCase):
    """Test case for the edit list view."""

    def setUp(self):
        """Set up test data for edit list view tests."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )
        self.list = CustomList.objects.create(name="Test List", owner=self.user)
        self.list.collaborators.add(self.collaborator)

    def test_edit_list(self):
        """Test editing an existing custom list."""
        self.client.login(**self.credentials)
        self.client.post(
            reverse("list_edit"),
            {
                "list_id": self.list.id,
                "name": "Updated List",
                "description": "Updated Description",
            },
        )
        self.list.refresh_from_db()
        self.assertEqual(self.list.name, "Updated List")
        self.assertEqual(self.list.description, "Updated Description")

    def test_edit_list_collaborator(self):
        """Test editing an existing custom list as a collaborator."""
        self.client.login(**self.collaborator_credentials)
        self.client.post(
            reverse("list_edit"),
            {
                "list_id": self.list.id,
                "name": "Updated List",
                "description": "Updated Description",
            },
        )
        self.list.refresh_from_db()
        self.assertEqual(self.list.name, "Updated List")
        self.assertEqual(self.list.description, "Updated Description")


class DeleteListViewTest(TestCase):
    """Test the delete view."""

    def setUp(self):
        """Create a user, log in, and create a list."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )
        self.list = CustomList.objects.create(name="Test List", owner=self.user)
        self.list.collaborators.add(self.collaborator)

    def test_delete_list(self):
        """Test deleting a list."""
        self.client.login(**self.credentials)
        self.client.post(reverse("list_delete"), {"list_id": self.list.id})
        self.assertEqual(CustomList.objects.count(), 0)

    def test_delete_list_collaborator(self):
        """Test deleting a list as a collaborator."""
        self.client.login(**self.collaborator_credentials)
        self.client.post(reverse("list_delete"), {"list_id": self.list.id})
        self.assertEqual(CustomList.objects.count(), 1)


class ReorderListItemViewTests(TestCase):
    """Tests for reordering items on a custom list."""

    def setUp(self):
        self.client = Client()
        self.owner = get_user_model().objects.create_user(
            username="owner",
            password="12345",
        )
        self.collaborator = get_user_model().objects.create_user(
            username="collab",
            password="12345",
        )
        self.outsider = get_user_model().objects.create_user(
            username="outsider",
            password="12345",
        )

        self.custom_list = CustomList.objects.create(
            name="Order Test",
            owner=self.owner,
        )
        self.custom_list.collaborators.add(self.collaborator)

        self.item_one = Item.objects.create(
            media_id="101",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="One",
        )
        self.item_two = Item.objects.create(
            media_id="102",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Two",
        )
        self.item_three = Item.objects.create(
            media_id="103",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Three",
        )

        custom_items = [
            CustomListItem.objects.create(custom_list=self.custom_list, item=self.item_one),
            CustomListItem.objects.create(custom_list=self.custom_list, item=self.item_two),
            CustomListItem.objects.create(custom_list=self.custom_list, item=self.item_three),
        ]
        start = timezone.now().replace(microsecond=0)
        for offset, custom_item in enumerate(custom_items):
            custom_item.date_added = start + timedelta(seconds=offset)
        CustomListItem.objects.bulk_update(custom_items, ["date_added"])

    def test_owner_can_move_item_to_first(self):
        self.client.login(username="owner", password="12345")
        response = self.client.post(
            reverse("list_reorder_item", args=[self.custom_list.id]),
            {"item_id": self.item_three.id, "action": "first"},
        )
        self.assertEqual(response.status_code, 204)

        ordered_ids = list(
            CustomListItem.objects.filter(custom_list=self.custom_list)
            .order_by("date_added", "id")
            .values_list("item_id", flat=True),
        )
        self.assertEqual(
            ordered_ids,
            [self.item_three.id, self.item_one.id, self.item_two.id],
        )

    def test_collaborator_can_reorder_items(self):
        self.client.login(username="collab", password="12345")
        response = self.client.post(
            reverse("list_reorder_item", args=[self.custom_list.id]),
            {"item_id": self.item_one.id, "action": "last"},
        )
        self.assertEqual(response.status_code, 204)

    def test_outsider_cannot_reorder_items(self):
        self.client.login(username="outsider", password="12345")
        response = self.client.post(
            reverse("list_reorder_item", args=[self.custom_list.id]),
            {"item_id": self.item_two.id, "action": "first"},
        )
        self.assertEqual(response.status_code, 403)


class ListsModalViewTests(TestCase):
    """Tests for the lists_modal view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

        # Create some test lists
        self.list1 = CustomList.objects.create(
            name="Test List 1",
            owner=self.user,
        )
        self.list2 = CustomList.objects.create(
            name="Test List 2",
            owner=self.user,
        )

    def test_lists_modal_view(self):
        """Test the basic lists_modal view."""
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, 10494],
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/fill_lists.html")
        self.assertIn("item", response.context)
        self.assertIn("custom_lists", response.context)
        self.assertIn("list_tags", response.context)

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_existing_item(
        self,
        mock_get_lists,
        mock_get_metadata,
    ):
        """Test the lists_modal view with an existing item."""
        # Create an existing item
        Item.objects.create(
            media_id="123",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Existing Movie",
            image="http://example.com/image.jpg",
        )

        # Mock the get_user_lists_with_item method
        mock_get_lists.return_value = [self.list1, self.list2]

        # Mock the get_media_metadata method
        mock_get_metadata.return_value = {
            "title": "Existing Movie",
            "image": "http://example.com/image.jpg",
        }

        # Test the view
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, "123"],
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/fill_lists.html")

        # Check context data
        self.assertEqual(response.context["item"].media_id, "123")
        self.assertEqual(response.context["item"].title, "Existing Movie")
        self.assertEqual(len(response.context["custom_lists"]), 2)

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_new_item(self, mock_get_lists, mock_get_metadata):
        """Test the lists_modal view with a new item."""
        # Mock the get_user_lists_with_item method
        mock_get_lists.return_value = [self.list1, self.list2]

        # Mock the get_media_metadata method
        mock_get_metadata.return_value = {
            "title": "New Movie",
            "image": "http://example.com/new_image.jpg",
        }

        # Test the view
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, "999"],
            ),
        )
        self.assertEqual(response.status_code, 200)

        # Check that a new item was created
        self.assertTrue(
            Item.objects.filter(media_id="999", source=Sources.TMDB.value).exists(),
        )
        new_item = Item.objects.get(media_id="999", source=Sources.TMDB.value)
        self.assertEqual(new_item.title, "New Movie")
        self.assertEqual(new_item.image, "http://example.com/new_image.jpg")

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_season(self, mock_get_lists, mock_get_metadata):
        """Test the lists_modal view with a season."""
        # Mock the get_user_lists_with_item method
        mock_get_lists.return_value = [self.list1, self.list2]

        # Mock the get_media_metadata method
        mock_get_metadata.return_value = {
            "title": "TV Show Season 1",
            "image": "http://example.com/season.jpg",
        }

        # Test the view
        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.SEASON.value, "123", "1"],
            ),
        )
        self.assertEqual(response.status_code, 200)

        # Check that a new item was created with season_number
        self.assertTrue(
            Item.objects.filter(
                media_id="123",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=1,
            ).exists(),
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_episode_uses_episode_title(
        self,
        mock_get_lists,
        mock_get_metadata,
    ):
        """Episode list items should store the episode title instead of the show title."""
        mock_get_lists.return_value = [self.list1, self.list2]
        mock_get_metadata.return_value = {
            "title": "Death Note",
            "episode_title": "Rebirth",
            "image": "http://example.com/episode.jpg",
        }

        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.EPISODE.value, "13916", "0", "1"],
            ),
        )

        self.assertEqual(response.status_code, 200)
        created_item = Item.objects.get(
            media_id="13916",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=0,
            episode_number=1,
        )
        self.assertEqual(created_item.title, "Rebirth")

    @patch("app.providers.services.get_media_metadata")
    @patch("lists.models.CustomList.objects.get_user_lists_with_item")
    def test_lists_modal_view_with_existing_episode_repairs_episode_title(
        self,
        mock_get_lists,
        mock_get_metadata,
    ):
        """Existing tracked episode rows should be repaired before rendering the modal."""
        mock_get_lists.return_value = [self.list1, self.list2]
        existing_item = Item.objects.create(
            media_id="13916",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Death Note",
            season_number=0,
            episode_number=1,
            image=settings.IMG_NONE,
        )
        mock_get_metadata.return_value = {
            "title": "Death Note",
            "season_title": "Specials",
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "Rebirth",
                    "image": settings.IMG_NONE,
                },
            ],
            "image": settings.IMG_NONE,
        }

        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.EPISODE.value, "13916", "0", "1"],
            ),
        )

        existing_item.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing_item.title, "Rebirth")

    def test_lists_modal_view_filters_lists_by_tag(self):
        """Tag query param should filter list options in the modal."""
        self.list1.tags = ["Active"]
        self.list1.save(update_fields=["tags"])
        self.list2.tags = ["Archive"]
        self.list2.save(update_fields=["tags"])

        response = self.client.get(
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.MOVIE.value, 10494],
            )
            + "?tag=Active",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_list_tag"], "Active")
        self.assertEqual(list(response.context["custom_lists"]), [self.list1])
        self.assertContains(response, "Test List 1")
        self.assertNotContains(response, "Test List 2")


class ListItemToggleTests(TestCase):
    """Tests for the list_item_toggle view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()

        # Create users
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.collaborator_credentials = {
            "username": "collaborator",
            "password": "12345",
        }
        self.collaborator = get_user_model().objects.create_user(
            **self.collaborator_credentials,
        )

        self.other_credentials = {
            "username": "otheruser",
            "password": "testpassword",
        }
        self.other_user = get_user_model().objects.create_user(
            **self.other_credentials,
        )

        # Create lists
        self.list = CustomList.objects.create(name="Test List", owner=self.user)
        self.list.collaborators.add(self.collaborator)

        # Create an item
        self.item = Item.objects.create(
            media_id=1,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    def test_list_item_owner_toggle(self):
        """Test adding an item to a list as owner."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.item, self.list.items.all())

    def test_list_item_owner_toggle_remove(self):
        """Test removing an item from a list as owner."""
        self.client.login(**self.credentials)
        self.list.items.add(self.item)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.item, self.list.items.all())

    def test_list_item_collaborator_toggle(self):
        """Test adding an item to a list as collaborator."""
        self.client.login(**self.collaborator_credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.item, self.list.items.all())

    def test_list_item_collaborator_toggle_remove(self):
        """Test removing an item from a list as collaborator."""
        self.client.login(**self.collaborator_credentials)
        self.list.items.add(self.item)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.item, self.list.items.all())

    def test_list_item_toggle_nonexistent_list(self):
        """Test toggling an item on a nonexistent list."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": 999,  # Nonexistent list
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_list_item_toggle_nonexistent_item(self):
        """Test toggling a nonexistent item."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": 999,  # Nonexistent item
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_list_item_toggle_unauthorized_list(self):
        """Test toggling an item on a list the user doesn't have access to."""
        self.client.login(**self.credentials)

        # Create a list owned by another user
        other_list = CustomList.objects.create(
            name="Other User's List",
            owner=self.other_user,
        )

        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": other_list.id,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_list_item_toggle_template_context(self):
        """Test the context data in the response template."""
        self.client.login(**self.credentials)
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_item_button.html")

        # Check context data
        self.assertEqual(response.context["custom_list"], self.list)
        self.assertEqual(response.context["item"], self.item)
        self.assertTrue(response.context["has_item"])  # Item was added

        # Toggle again to remove
        response = self.client.post(
            reverse("list_item_toggle"),
            {
                "item_id": self.item.id,
                "custom_list_id": self.list.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_item"])  # Item was removed


class ListRssFeedTests(TestCase):
    """Tests for the public list RSS feed."""

    def setUp(self):
        """Set up test data."""
        self.user = get_user_model().objects.create_user(
            username="rssuser",
            password="testpassword",
        )
        self.custom_list = CustomList.objects.create(
            name="Public RSS List",
            description="Test RSS list",
            owner=self.user,
            visibility="public",
        )
        self.movie_item = Item.objects.create(
            media_id="rss-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="RSS Movie",
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.movie_item,
        )

    def test_public_list_rss_feed(self):
        """Return RSS feed for a public list."""
        response = self.client.get(reverse("list_rss", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"rss", response.content)
        self.assertIn(b"RSS Movie", response.content)

    def test_private_list_rss_feed_returns_404(self):
        """Return 404 for private lists."""
        self.custom_list.visibility = "private"
        self.custom_list.save(update_fields=["visibility"])

        response = self.client.get(reverse("list_rss", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 404)


class ListJsonExportTests(TestCase):
    """Tests for the public list JSON export endpoints."""

    def setUp(self):
        """Set up test data."""
        self.user = get_user_model().objects.create_user(
            username="jsonuser",
            password="testpassword",
        )
        self.custom_list = CustomList.objects.create(
            name="Public JSON List",
            description="Test JSON list",
            owner=self.user,
            visibility="public",
        )
        # Create TMDB movie
        self.movie_item = Item.objects.create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.movie_item,
        )
        # Create TMDB TV show
        self.tv_item = Item.objects.create(
            media_id="67890",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.tv_item,
        )
        # Create non-TMDB item (should be excluded)
        self.manual_item = Item.objects.create(
            media_id="manual-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Manual Movie",
        )
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.manual_item,
        )

    def test_radarr_json_format(self):
        """Return JSON in Radarr format for public list."""
        response = self.client.get(
            reverse("list_json", args=[self.custom_list.id]) + "?arr=radarr",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        data = response.json()
        self.assertIsInstance(data, list)
        # Should only include TMDB movies
        self.assertEqual(len(data), 1)
        self.assertIn({"id": 12345}, data)

    def test_sonarr_json_format(self):
        """Return JSON in Sonarr format for public list."""
        # Mock TMDB TV metadata with TVDB ID
        from unittest.mock import patch

        mock_metadata = {
            "media_id": "67890",
            "title": "Test TV Show",
            "tvdb_id": 81189,
        }

        with patch("lists.feeds.tmdb.tv", return_value=mock_metadata):
            response = self.client.get(
                reverse("list_json", args=[self.custom_list.id]) + "?arr=sonarr",
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/json")
            data = response.json()
            self.assertIsInstance(data, list)
            # Should only include TMDB TV shows with TVDB IDs
            self.assertEqual(len(data), 1)
            self.assertIn({"tvdbId": 81189}, data)

    def test_sonarr_skips_items_without_tvdb_id(self):
        """Sonarr endpoint skips TV shows without TVDB ID mapping."""
        from unittest.mock import patch

        # Mock TMDB TV metadata without TVDB ID
        mock_metadata = {
            "media_id": "67890",
            "title": "Test TV Show",
            "tvdb_id": None,
        }

        with patch("lists.feeds.tmdb.tv", return_value=mock_metadata):
            response = self.client.get(
                reverse("list_json", args=[self.custom_list.id]) + "?arr=sonarr",
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            # Should be empty since TV show has no TVDB ID
            self.assertEqual(len(data), 0)

    def test_private_list_json_returns_404(self):
        """Return 404 for private lists."""
        self.custom_list.visibility = "private"
        self.custom_list.save(update_fields=["visibility"])

        response = self.client.get(
            reverse("list_json", args=[self.custom_list.id]) + "?arr=radarr",
        )

        self.assertEqual(response.status_code, 404)

    def test_json_filters_by_media_type(self):
        """JSON endpoints filter items by media type correctly."""
        # Radarr should only return movies
        response = self.client.get(
            reverse("list_json", args=[self.custom_list.id]) + "?arr=radarr",
        )
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], 12345)

        # Sonarr should only return TV shows
        from unittest.mock import patch

        mock_metadata = {
            "media_id": "67890",
            "title": "Test TV Show",
            "tvdb_id": 81189,
        }

        with patch("lists.feeds.tmdb.tv", return_value=mock_metadata):
            response = self.client.get(
                reverse("list_json", args=[self.custom_list.id]) + "?arr=sonarr",
            )
            data = response.json()
            self.assertEqual(len(data), 1)
            self.assertIn("tvdbId", data[0])

    def test_json_only_includes_tmdb_items(self):
        """JSON endpoints only include items from TMDB source."""
        response = self.client.get(
            reverse("list_json", args=[self.custom_list.id]) + "?arr=radarr",
        )
        data = response.json()
        # Should not include manual item
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], 12345)

    def test_missing_arr_parameter_returns_error(self):
        """Missing arr parameter returns 400 error."""
        response = self.client.get(reverse("list_json", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    def test_invalid_arr_parameter_returns_error(self):
        """Invalid arr parameter returns 400 error."""
        response = self.client.get(
            reverse("list_json", args=[self.custom_list.id]) + "?arr=invalid",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)


class RecommendationRedirectTests(TestCase):
    """Tests for recommendation flow redirect behavior."""

    def setUp(self):
        self.client = Client()
        self.custom_list = CustomList.objects.create(
            name="Public Recs",
            owner=get_user_model().objects.create_user("owner", "owner@example.com", "pw"),
            visibility="public",
            allow_recommendations=True,
        )
        self.item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Recommendation Target",
            image="https://example.com/poster.jpg",
        )

    def test_submit_recommendation_redirects_to_next_search_page(self):
        """Recommendation submit should preserve recommendation search page via next."""
        next_url = f"{reverse('recommend_item', args=[self.custom_list.id])}?q=dark&media_type=movie&page=2"
        response = self.client.post(
            reverse("submit_recommendation", args=[self.custom_list.id]),
            {
                "media_id": self.item.media_id,
                "media_type": self.item.media_type,
                "source": self.item.source,
                "next": next_url,
            },
        )

        self.assertRedirects(response, next_url, fetch_redirect_response=False)

    def test_submit_recommendation_ignores_external_next_url(self):
        """External next URLs should be rejected for security."""
        response = self.client.post(
            reverse("submit_recommendation", args=[self.custom_list.id]),
            {
                "media_id": self.item.media_id,
                "media_type": self.item.media_type,
                "source": self.item.source,
                "next": "https://evil.example/path",
            },
        )

        self.assertRedirects(
            response,
            reverse("list_detail", args=[self.custom_list.id]),
            fetch_redirect_response=False,
        )


class QuickAddListItemTests(TestCase):
    """Tests for the owner quick-add list search flow."""

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user("owner", "owner@example.com", "pw")
        self.client.force_login(self.user)
        self.custom_list = CustomList.objects.create(
            name="Manual List",
            owner=self.user,
        )
        self.smart_list = CustomList.objects.create(
            name="Smart List",
            owner=self.user,
            is_smart=True,
        )
        self.item = Item.objects.create(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Quick Add Target",
            image="https://example.com/poster.jpg",
        )

    def test_add_list_item_page_uses_add_template(self):
        """Editable manual lists should render the quick-add search page."""
        response = self.client.get(reverse("list_add_item", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/add_item.html")

    def test_add_list_item_page_redirects_for_smart_lists(self):
        """Smart lists should redirect back to detail instead of opening quick add."""
        response = self.client.get(reverse("list_add_item", args=[self.smart_list.id]))

        self.assertRedirects(
            response,
            reverse("list_detail", args=[self.smart_list.id]),
            fetch_redirect_response=False,
        )

    @patch("lists.views.services.get_media_metadata")
    def test_add_list_item_search_preview_renders_owner_add_modal(self, mock_get_metadata):
        """Preview requests should use the direct-add modal instead of recommendations."""
        mock_get_metadata.return_value = {
            "title": "Preview Movie",
            "image": "https://example.com/preview.jpg",
            "details": {},
            "genres": [],
            "synopsis": "",
        }

        response = self.client.get(
            reverse("list_add_item_search", args=[self.custom_list.id]),
            {
                "show_preview": "true",
                "media_id": self.item.media_id,
                "media_type": self.item.media_type,
                "source": self.item.source,
                "q": "dark",
                "search_media_type": self.item.media_type,
                "page": 2,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/add_item_preview_modal.html")
        self.assertContains(response, reverse("list_add_item_submit", args=[self.custom_list.id]))
        self.assertContains(
            response,
            f'{reverse("list_add_item", args=[self.custom_list.id])}?q=dark&amp;media_type=movie&amp;page=2',
        )

    @patch("lists.views.services.get_media_metadata")
    def test_add_list_item_search_preview_preserves_episode_identity_fields(
        self,
        mock_get_metadata,
    ):
        """Episode preview forms should keep season/episode identity on submit."""
        mock_get_metadata.return_value = {
            "title": "Death Note",
            "episode_title": "Rebirth",
            "image": "https://example.com/episode.jpg",
            "details": {},
            "genres": [],
            "synopsis": "",
        }

        response = self.client.get(
            reverse("list_add_item_search", args=[self.custom_list.id]),
            {
                "show_preview": "true",
                "media_id": "1668",
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TMDB.value,
                "season_number": 1,
                "episode_number": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="season_number" value="1"', html=False)
        self.assertContains(response, 'name="episode_number" value="1"', html=False)

    def test_add_list_item_submit_redirects_to_next_search_page(self):
        """Quick-add submit should preserve the search page via next."""
        next_url = (
            f"{reverse('list_add_item', args=[self.custom_list.id])}"
            "?q=dark&media_type=movie&page=2"
        )
        response = self.client.post(
            reverse("list_add_item_submit", args=[self.custom_list.id]),
            {
                "media_id": self.item.media_id,
                "media_type": self.item.media_type,
                "source": self.item.source,
                "next": next_url,
            },
        )

        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.assertTrue(
            CustomListItem.objects.filter(custom_list=self.custom_list, item=self.item).exists(),
        )
        self.assertTrue(
            ListActivity.objects.filter(
                custom_list=self.custom_list,
                item=self.item,
            ).exists(),
        )

    @patch("lists.views.services.get_media_metadata")
    def test_add_list_item_submit_creates_episode_item_with_identity_fields(
        self,
        mock_get_metadata,
    ):
        """Episode quick-add submits should create the episode-specific Item row."""
        mock_get_metadata.return_value = {
            "title": "Death Note",
            "episode_title": "Rebirth",
            "image": "https://example.com/episode.jpg",
            "details": {},
            "genres": [],
            "synopsis": "",
        }
        next_url = f"{reverse('list_add_item', args=[self.custom_list.id])}?q=death+note"

        response = self.client.post(
            reverse("list_add_item_submit", args=[self.custom_list.id]),
            {
                "media_id": "1668",
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TMDB.value,
                "season_number": 1,
                "episode_number": 1,
                "next": next_url,
            },
        )

        episode_item = Item.objects.get(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
        )

        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.assertEqual(episode_item.title, "Rebirth")
        self.assertTrue(
            CustomListItem.objects.filter(
                custom_list=self.custom_list,
                item=episode_item,
            ).exists(),
        )

    def test_add_list_item_submit_ignores_external_next_url(self):
        """External next URLs should be rejected for security."""
        response = self.client.post(
            reverse("list_add_item_submit", args=[self.custom_list.id]),
            {
                "media_id": self.item.media_id,
                "media_type": self.item.media_type,
                "source": self.item.source,
                "next": "https://evil.example/path",
            },
        )

        self.assertRedirects(
            response,
            reverse("list_detail", args=[self.custom_list.id]),
            fetch_redirect_response=False,
        )
