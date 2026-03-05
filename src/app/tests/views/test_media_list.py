import json
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Book,
    Comic,
    CollectionEntry,
    Game,
    Item,
    Manga,
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
        self.assertContains(response, "toggleSort('release_date')")
        self.assertContains(response, "toggleSort('date_added')")

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

    def test_movie_sort_by_release_date_orders_items(self):
        """Release date sort should order by item.release_datetime."""
        now = timezone.now()
        item1 = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        item2 = Item.objects.get(
            title="Test Movie 2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        item3 = Item.objects.get(
            title="Test Movie 3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        Item.objects.filter(id=item1.id).update(release_datetime=now - timedelta(days=90))
        Item.objects.filter(id=item2.id).update(release_datetime=now - timedelta(days=15))
        Item.objects.filter(id=item3.id).update(release_datetime=now - timedelta(days=45))

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?sort=release_date&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "release_date")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "Test Movie 1")

    def test_movie_sort_by_date_added_orders_items(self):
        """Date added sort should order by media.created_at."""
        oldest = timezone.now() - timedelta(days=120)
        newest = timezone.now() - timedelta(days=3)
        middle = timezone.now() - timedelta(days=40)

        movie1 = Movie.objects.get(item__title="Test Movie 1", user=self.user)
        movie2 = Movie.objects.get(item__title="Test Movie 2", user=self.user)
        movie3 = Movie.objects.get(item__title="Test Movie 3", user=self.user)
        Movie.objects.filter(id=movie1.id).update(created_at=oldest)
        Movie.objects.filter(id=movie2.id).update(created_at=newest)
        Movie.objects.filter(id=movie3.id).update(created_at=middle)

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?sort=date_added&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "date_added")
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

    def test_game_platform_filter_prefers_collection_resolution(self):
        """Game platform filtering should prefer collection platform over metadata platforms."""
        switch_override_item = Item.objects.create(
            media_id="game-platform-filter-1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Multiplatform Game",
            image="http://example.com/game1.jpg",
            platforms=["PlayStation 5"],
        )
        ps5_item = Item.objects.create(
            media_id="game-platform-filter-2",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="PS5 Exclusive Game",
            image="http://example.com/game2.jpg",
            platforms=["PlayStation 5"],
        )

        Game.objects.bulk_create(
            [
                Game(
                    item=switch_override_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=60,
                ),
                Game(
                    item=ps5_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=60,
                ),
            ],
        )

        CollectionEntry.objects.create(
            user=self.user,
            item=switch_override_item,
            resolution="Nintendo Switch",
        )

        url = reverse("medialist", args=[MediaTypes.GAME.value])
        response = self.client.get(url, {"platform": "PlayStation 5", "status": "All"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "PS5 Exclusive Game")
        self.assertNotContains(response, "Multiplatform Game")

        platform_values = {
            option["value"] for option in response.context["filter_data"]["platforms"]
        }
        self.assertIn("Nintendo Switch", platform_values)
        self.assertIn("PlayStation 5", platform_values)

    def test_game_platform_filter_uses_latest_aggregated_status(self):
        """Status filtering should honor latest aggregated status for duplicate sessions."""
        stale_item = Item.objects.create(
            media_id="game-platform-filter-latest-1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Completed Now, Was In Progress",
            image="http://example.com/game-latest-1.jpg",
            platforms=["PlayStation 5"],
        )
        active_item = Item.objects.create(
            media_id="game-platform-filter-latest-2",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Still In Progress",
            image="http://example.com/game-latest-2.jpg",
            platforms=["PlayStation 5"],
        )

        old_in_progress = Game.objects.create(
            item=stale_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=12,
        )
        old_activity = timezone.now() - timedelta(days=3)
        Game.objects.filter(id=old_in_progress.id).update(
            created_at=old_activity,
            progressed_at=old_activity,
        )

        Game.objects.create(
            item=stale_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=30,
            end_date=timezone.now() - timedelta(days=1),
        )

        Game.objects.create(
            item=active_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=20,
        )

        url = reverse("medialist", args=[MediaTypes.GAME.value])
        response = self.client.get(
            url,
            {"platform": "PlayStation 5", "status": Status.IN_PROGRESS.value},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "Still In Progress")
        self.assertNotContains(response, "Completed Now, Was In Progress")

    def test_book_format_filter_uses_collection_entry_media_type(self):
        """Book format options should include collection-only formats like Audiobook."""
        book_item = Item.objects.create(
            media_id="book-audiobook-filter",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Audiobook Filter Book",
            image="http://example.com/book.jpg",
            format="",
        )
        Book.objects.create(
            item=book_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=book_item,
            media_type="Audiobook",
        )

        url = reverse("medialist", args=[MediaTypes.BOOK.value])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_data"]["show_formats"])
        self.assertTrue(
            any(
                option["value"] == "audiobook"
                and option["label"] == "Audiobook"
                for option in response.context["filter_data"]["formats"]
            ),
        )

        filtered_response = self.client.get(f"{url}?format=audiobook")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertEqual(filtered_response.context["current_format"], "audiobook")
        self.assertEqual(filtered_response.context["media_list"].paginator.count, 1)
        self.assertContains(filtered_response, "Audiobook Filter Book")

    def test_book_author_filter_shows_and_filters_tracked_books(self):
        book_with_author = Item.objects.create(
            media_id="book-author-filter-1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Filter Book One",
            image="http://example.com/book1.jpg",
            authors=["Author One"],
        )
        other_book = Item.objects.create(
            media_id="book-author-filter-2",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Filter Book Two",
            image="http://example.com/book2.jpg",
            authors=["Author Two"],
        )
        Book.objects.create(
            item=book_with_author,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        Book.objects.create(
            item=other_book,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        url = reverse("medialist", args=[MediaTypes.BOOK.value])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_data"]["show_authors"])
        self.assertTrue(
            any(
                option["value"] == "Author One"
                for option in response.context["filter_data"]["authors"]
            ),
        )

        filtered_response = self.client.get(f"{url}?author=Author One")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertEqual(filtered_response.context["current_author"], "Author One")
        self.assertEqual(filtered_response.context["media_list"].paginator.count, 1)
        self.assertContains(filtered_response, "Author Filter Book One")
        self.assertNotContains(filtered_response, "Author Filter Book Two")

    def test_comic_and_manga_author_filter_work(self):
        comic_item = Item.objects.create(
            media_id="comic-author-filter-1",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
            title="Author Filter Comic",
            image="http://example.com/comic.jpg",
            authors=["Writer Alpha"],
        )
        manga_item = Item.objects.create(
            media_id="manga-author-filter-1",
            source=Sources.MANGAUPDATES.value,
            media_type=MediaTypes.MANGA.value,
            title="Author Filter Manga",
            image="http://example.com/manga.jpg",
            authors=["Mangaka Beta"],
        )
        Comic.objects.create(
            item=comic_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        Manga.objects.create(
            item=manga_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        comic_url = reverse("medialist", args=[MediaTypes.COMIC.value])
        comic_response = self.client.get(f"{comic_url}?author=Writer Alpha")
        self.assertEqual(comic_response.status_code, 200)
        self.assertTrue(comic_response.context["filter_data"]["show_authors"])
        self.assertEqual(comic_response.context["media_list"].paginator.count, 1)
        self.assertContains(comic_response, "Author Filter Comic")

        manga_url = reverse("medialist", args=[MediaTypes.MANGA.value])
        manga_response = self.client.get(f"{manga_url}?author=Mangaka Beta")
        self.assertEqual(manga_response.status_code, 200)
        self.assertTrue(manga_response.context["filter_data"]["show_authors"])
        self.assertEqual(manga_response.context["media_list"].paginator.count, 1)
        self.assertContains(manga_response, "Author Filter Manga")

    def test_movie_filter_data_hides_author_filter(self):
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["filter_data"]["show_authors"])

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=grid",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/table_items.html")

    def test_table_column_refresh_wiring_is_present(self):
        """Table layout should render deterministic column refresh wiring."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )

        self.assertContains(response, 'id="column-pref-form"')
        self.assertContains(response, "this.$refs.form.addEventListener('htmx:afterRequest'")
        self.assertContains(response, "htmx.ajax('GET'")
        self.assertContains(response, "column_refresh_nonce")
        self.assertContains(response, "save_after_request successful=")
        self.assertContains(response, "refresh_dispatch source=save_after_request")
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
                "order": ["status", "score", "release_date", "date_added", "start_date", "end_date"],
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
            ["score", "start_date", "status", "release_date", "date_added", "end_date"],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertEqual(
            column_keys,
            ["image", "title", "score", "start_date", "status", "release_date", "date_added", "end_date"],
        )

    def test_consecutive_column_reorder_full_round_trip(self):
        """Two consecutive column saves should each render after HTMX refresh."""
        columns_url = reverse("medialist_columns", args=[MediaTypes.MOVIE.value])
        list_url = reverse("medialist", args=[MediaTypes.MOVIE.value])
        list_query = "?layout=table&sort=score&direction=desc"
        htmx_headers = {"HTTP_HX_REQUEST": "true"}

        def assert_partial_table_refresh(response, expected_labels):
            self.assertEqual(response.status_code, 200)
            self.assertIn("HX-Trigger", response)
            trigger_payload = json.loads(response["HX-Trigger"])
            self.assertIn("resultCountUpdated", trigger_payload)

            html = response.content.decode()
            labels = [
                re.sub(r"<[^>]+>", "", label).strip()
                for label in re.findall(
                    r"<th\s[^>]*>(.*?)</th>",
                    html,
                    flags=re.DOTALL,
                )
            ]
            self.assertEqual(labels, expected_labels)

        first_order = ["end_date", "status", "score", "start_date"]
        first_save = self.client.post(
            columns_url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(first_order),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(first_save.status_code, 204)
        self.assertIn("HX-Trigger", first_save)
        self.assertEqual(
            json.loads(first_save["HX-Trigger"]),
            {"refreshTableColumns": True},
        )

        first_refresh = self.client.get(f"{list_url}{list_query}", **htmx_headers)
        assert_partial_table_refresh(
            first_refresh,
            ["", "Title", "End Date", "Status", "Score", "Start Date", "Release Date", "Date Added"],
        )

        second_order = ["score", "start_date", "end_date", "status"]
        second_save = self.client.post(
            columns_url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(second_order),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(second_save.status_code, 204)
        self.assertIn("HX-Trigger", second_save)
        self.assertEqual(
            json.loads(second_save["HX-Trigger"]),
            {"refreshTableColumns": True},
        )

        second_refresh = self.client.get(f"{list_url}{list_query}", **htmx_headers)
        assert_partial_table_refresh(
            second_refresh,
            ["", "Title", "Score", "Start Date", "End Date", "Status", "Release Date", "Date Added"],
        )

        full_page = self.client.get(f"{list_url}{list_query}")
        self.assertEqual(full_page.status_code, 200)
        self.assertContains(full_page, 'id="column-pref-form"')
        self.assertContains(full_page, f'hx-post="{columns_url}"')
        self.assertContains(full_page, 'hx-swap="none"')
        self.assertContains(full_page, 'x-data="columnConfigMenu()"')
        self.assertContains(full_page, "refreshMediaTableAfterColumnSave()")

        full_html = full_page.content.decode()
        config_match = re.search(
            r'<script id="media-column-config-data" type="application/json">'
            r"(.*?)</script>",
            full_html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(config_match)
        if config_match is not None:
            column_config = json.loads(config_match.group(1))
            self.assertEqual(
                [column["key"] for column in column_config],
                second_order + ["release_date", "date_added"],
            )

        resolved_keys = [column.key for column in full_page.context["resolved_columns"]]
        self.assertEqual(
            resolved_keys,
            ["image", "title", "score", "start_date", "end_date", "status", "release_date", "date_added"],
        )
