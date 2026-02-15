import json
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)
from app.templatetags import app_tags


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        Item.objects.bulk_create(
            [
                Item(
                    media_id=movies_id[i - 1],
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"Test Movie {i}",
                    image="http://example.com/image.jpg",
                )
                for i in range(1, 6)
            ],
        )
        created_items = {
            item.media_id: item
            for item in Item.objects.filter(
                media_id__in=movies_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            )
        }

        Movie.objects.bulk_create(
            [
                Movie(
                    item=created_items[movies_id[i - 1]],
                    user=self.user,
                    status=(
                        Status.COMPLETED.value
                        if i < num_completed
                        else Status.IN_PROGRESS.value
                    ),
                    progress=1 if i < num_completed else 0,
                    score=i,
                )
                for i in range(1, 6)
            ],
        )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_movie_grid_aggregates_duplicate_completed_plays(self):
        """Grid cards should show total plays across duplicate completed movie entries."""
        item = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        existing_play = Movie.objects.get(item=item, user=self.user)
        existing_play.score = 9
        existing_play.save(update_fields=["score"])

        second_date = timezone.now() - timedelta(days=7)
        third_date = timezone.now() - timedelta(days=1)
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    score=None,
                    start_date=second_date,
                    end_date=second_date,
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    score=9,
                    start_date=third_date,
                    end_date=third_date,
                ),
            ],
        )

        latest = Movie.objects.filter(item=item, user=self.user).order_by("-id").first()
        latest.score = 10
        latest.save(update_fields=["score"])

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Test+Movie+1&sort=title&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "Test Movie 1")
        self.assertContains(response, "3 plays")

    def test_movie_grid_counts_completed_plays_when_progress_is_zero(self):
        """Completed movie duplicates should count as plays even when progress is zero."""
        item = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )

        first_play = Movie.objects.get(item=item, user=self.user)
        first_play.status = Status.COMPLETED.value
        first_play.progress = 1
        first_play.end_date = timezone.now() - timedelta(days=220)
        first_play.save()

        second_date = timezone.now() - timedelta(days=126)
        third_date = timezone.now() - timedelta(days=90)
        fourth_date = timezone.now() - timedelta(days=9)
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=second_date,
                ),
                # Simulate legacy/completed rows where progress was never normalized to 1.
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=0,
                    end_date=third_date,
                    score=9,
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=0,
                    end_date=fourth_date,
                    score=10,
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Test+Movie+1&sort=title&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "Test Movie 1")
        self.assertContains(response, "4 plays")

    def test_movie_sort_dropdown_includes_plays_option(self):
        """Movie sort dropdown should include the plays sort option."""
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "toggleSort('plays')")

    def test_non_movie_sort_hides_plays_option_and_falls_back(self):
        """Non-movie media types should hide plays sort and fallback to title."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.ANIME.value]) + "?sort=plays",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('plays')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.anime_sort, "title")

    def test_movie_sort_by_plays_orders_by_aggregated_completed_plays(self):
        """Movie plays sort should use aggregated completed play totals."""
        item = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        older = timezone.now() - timedelta(days=30)
        newer = timezone.now() - timedelta(days=3)
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=0,
                    end_date=older,
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=newer,
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?sort=plays&direction=desc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "plays")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "Test Movie 1")

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        self.assertEqual(response.context["media_list"].paginator.count, 2)

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_media_list_with_release_filters(self):
        """Release filter should split tracked media by today."""
        now = timezone.now()
        released_item = (
            Item.objects.filter(
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title="Test Movie 1",
            )
            .only("id")
            .first()
        )
        upcoming_item = (
            Item.objects.filter(
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title="Test Movie 2",
            )
            .only("id")
            .first()
        )
        self.assertIsNotNone(released_item)
        self.assertIsNotNone(upcoming_item)
        Item.objects.filter(id=released_item.id).update(
            release_datetime=now - timedelta(days=30),
        )
        Item.objects.filter(id=upcoming_item.id).update(
            release_datetime=now + timedelta(days=30),
        )

        url = reverse("medialist", args=[MediaTypes.MOVIE.value])

        released_response = self.client.get(f"{url}?release=released")
        self.assertEqual(released_response.status_code, 200)
        self.assertEqual(released_response.context["current_release"], "released")
        self.assertEqual(released_response.context["media_list"].paginator.count, 1)
        self.assertContains(released_response, "Test Movie 1")
        self.assertNotContains(released_response, "Test Movie 2")

        not_released_response = self.client.get(f"{url}?release=not_released")
        self.assertEqual(not_released_response.status_code, 200)
        self.assertEqual(
            not_released_response.context["current_release"],
            "not_released",
        )
        self.assertEqual(not_released_response.context["media_list"].paginator.count, 4)
        self.assertContains(not_released_response, "Test Movie 2")
        self.assertNotContains(not_released_response, "Test Movie 1")

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        headers = {"HTTP_HX_REQUEST": "true"}

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=grid",
            **headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
            **headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/table_items.html")

    def test_table_column_refresh_wiring_is_present(self):
        """Table layout should render deterministic column refresh wiring."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )

        self.assertContains(response, 'id="column-pref-form"')
        self.assertContains(response, "elt.id !== 'column-pref-form'")
        self.assertContains(response, "htmx.ajax('GET'")
        self.assertContains(response, "refresh_dispatch source=afterRequest")
        self.assertNotContains(response, 'id="column-refresh-runner"')
        self.assertNotContains(response, 'hx-trigger="runColumnRefresh"')

    def test_table_header_and_row_cells_match_for_pagination(self):
        """Table pagination rows should always match header column count."""
        extra_ids = [f"extra-{i}" for i in range(6, 41)]
        Item.objects.bulk_create(
            [
                Item(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"Extra Movie {media_id}",
                    image="http://example.com/image.jpg",
                )
                for media_id in extra_ids
            ],
        )
        extra_items = {
            item.media_id: item
            for item in Item.objects.filter(
                media_id__in=extra_ids,
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            )
        }
        Movie.objects.bulk_create(
            [
                Movie(
                    item=extra_items[media_id],
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                    score=5,
                )
                for media_id in extra_ids
            ],
        )

        headers = {"HTTP_HX_REQUEST": "true"}
        first_page = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table&page=1",
            **headers,
        )
        first_html = first_page.content.decode()
        header_count = first_html.count("<th ")
        self.assertGreater(header_count, 0)

        first_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", first_html, flags=re.S)
        self.assertGreater(len(first_rows), 0)
        for row_html in first_rows:
            if "<td " not in row_html:
                continue
            self.assertEqual(row_html.count("<td "), header_count)

        second_page = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table&page=2",
            **headers,
        )
        second_html = second_page.content.decode()
        self.assertNotIn("<thead", second_html)

        second_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", second_html, flags=re.S)
        self.assertGreater(len(second_rows), 0)
        for row_html in second_rows:
            self.assertEqual(row_html.count("<td "), header_count)

    def test_column_preferences_endpoint_updates_user_prefs(self):
        """Column preference updates should persist and trigger table refresh."""
        response = self.client.post(
            reverse("medialist_columns", args=[MediaTypes.MOVIE.value]),
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["status"]),
                "hidden": json.dumps(["status"]),
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("HX-Trigger", response)
        self.assertIn("refreshTableColumns", response["HX-Trigger"])

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.MOVIE.value],
            {
                "order": ["status", "score", "start_date", "end_date"],
                "hidden": ["status"],
            },
        )

    def test_table_columns_keep_fixed_columns_at_front_after_save(self):
        """Saving prefs without fixed columns in order keeps them anchored first."""
        self.client.post(
            reverse("medialist_columns", args=[MediaTypes.MOVIE.value]),
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["end_date", "status"]),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertEqual(column_keys[:2], ["image", "title"])
        self.assertEqual(column_keys[2:4], ["end_date", "status"])

    def test_column_preferences_second_save_wins(self):
        """Consecutive saves should persist and render the latest submitted order."""
        url = reverse("medialist_columns", args=[MediaTypes.MOVIE.value])
        first = self.client.post(
            url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["status", "end_date"]),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(first.status_code, 204)

        second = self.client.post(
            url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["score", "start_date"]),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(second.status_code, 204)

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.MOVIE.value]["order"],
            ["score", "start_date", "status", "end_date"],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertEqual(
            column_keys,
            ["image", "title", "score", "start_date", "status", "end_date"],
        )
