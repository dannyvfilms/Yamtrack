from datetime import timedelta
import re
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import history_cache, statistics_cache
from app.models import (
    Anime,
    Book,
    Comic,
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Episode,
    Game,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Manga,
    Person,
    PersonGender,
    Season,
    Sources,
    Status,
    Studio,
    TV,
)


class StatisticsViewTests(TestCase):
    """Test the statistics view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_statistics_view_default_date_range(self):
        """Test the statistics view with default date range (last year)."""
        # Call the view
        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/statistics.html")

        self.assertIn("media_count", response.context)
        self.assertIn("activity_data", response.context)
        self.assertIn("media_type_distribution", response.context)
        self.assertIn("score_distribution", response.context)
        self.assertIn("status_distribution", response.context)
        self.assertIn("status_pie_chart_data", response.context)
        self.assertIn("daily_hours_by_media_type", response.context)

    def test_statistics_view_custom_date_range(self):
        """Test the statistics view with custom date range."""
        start_date = "2023-01-01"
        end_date = "2023-12-31"

        # Call the view with custom date range
        response = self.client.get(
            reverse("statistics") + f"?start-date={start_date}&end-date={end_date}",
        )

        self.assertEqual(response.status_code, 200)

        self.assertIn("media_count", response.context)
        self.assertIn("activity_data", response.context)
        self.assertIn("media_type_distribution", response.context)
        self.assertIn("score_distribution", response.context)
        self.assertIn("status_distribution", response.context)
        self.assertIn("status_pie_chart_data", response.context)
        self.assertIn("daily_hours_by_media_type", response.context)

    def test_statistics_view_invalid_date_format(self):
        """Test the statistics view with invalid date format."""
        start_date = "01/01/2023"  # MM/DD/YYYY instead of YYYY-MM-DD
        end_date = "2023/12/31"

        # Call the view with invalid date format
        response = self.client.get(
            reverse("statistics") + f"?start-date={start_date}&end-date={end_date}",
        )

        self.assertEqual(response.status_code, 200)

        date_is_none = (
            response.context["start_date"] is None
            and response.context["end_date"] is None
        )

        self.assertTrue(date_is_none)

    def test_refresh_statistics_cache_game_daily_average_tooltip_uses_game_title(self):
        """Cached game daily-average tooltip payload should include resolved game titles."""
        cache.clear()
        now = timezone.now()
        game_item = Item.objects.create(
            media_id="tooltip-game-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Tooltip Game",
            image="http://example.com/tooltip-game.jpg",
            platforms=["PlayStation 5"],
        )
        Game.objects.create(
            user=self.user,
            item=game_item,
            status=Status.IN_PROGRESS.value,
            progress=84,
            start_date=now,
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        stats_data = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        self.assertIsNotNone(stats_data)

        by_daily_average = stats_data["game_consumption"]["charts"]["by_daily_average"]
        top_games_per_band = by_daily_average["top_games_per_band"]
        all_titles = [
            game["title"]
            for games in top_games_per_band.values()
            for game in games
        ]
        self.assertIn("Tooltip Game", all_titles)

        platform_breakdown = stats_data["game_consumption"]["platform_breakdown"]
        self.assertTrue(platform_breakdown)
        self.assertEqual(platform_breakdown[0]["name"], "PlayStation 5")

    def test_refresh_statistics_cache_handles_anime_date_ranges(self):
        """Refreshing cache should not crash for anime entries with both start and end dates."""
        cache.clear()
        now = timezone.now()
        anime_item = Item.objects.create(
            media_id="anime-range-1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Range Anime",
            image="http://example.com/range-anime.jpg",
            runtime_minutes=24,
            genres=["Action"],
        )
        Anime.objects.create(
            user=self.user,
            item=anime_item,
            status=Status.PLANNING.value,
            progress=12,
            start_date=now - timedelta(days=3),
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        stats_data = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        self.assertIsNotNone(stats_data)

    def test_statistics_view_average_rating_uses_user_rating_scale(self):
        """Average rating card should use the configured user rating scale."""
        cache.clear()
        self.client.login(**self.credentials)
        self.user.rating_scale = "5"
        self.user.save(update_fields=["rating_scale"])

        now = timezone.now()
        item = Item.objects.create(
            media_id="movie-rating-scale-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Rating Scale Movie",
            image="http://example.com/rating-scale-movie.jpg",
        )
        Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=now,
            end_date=now,
            score=8,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        score_distribution = response.context["score_distribution"]
        self.assertEqual(score_distribution["scale_max"], 5)
        self.assertEqual(score_distribution["average_score"], 4.0)
        self.assertEqual(score_distribution["labels"], [str(score) for score in range(6)])

        response_body = response.content.decode()
        self.assertRegex(
            response_body,
            re.compile(
                r"Average Rating.*?4(?:\.0+)?\s*<span[^>]*>/\s*5</span>",
                re.DOTALL,
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_passes_reading_top_genres_for_book_comic_manga(self, mock_get_metadata):
        """Book/comic/manga genre rollups should be exposed in consumption context."""
        mock_get_metadata.return_value = {"max_progress": 2000}
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        book_item = Item.objects.create(
            media_id="book-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Genre Test",
            image="http://example.com/book.jpg",
            genres=["Fantasy", "Adventure"],
        )
        comic_item = Item.objects.create(
            media_id="comic-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Comic Genre Test",
            image="http://example.com/comic.jpg",
            genres=["Sci-Fi"],
        )
        manga_item = Item.objects.create(
            media_id="manga-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga Genre Test",
            image="http://example.com/manga.jpg",
            genres=["Shonen"],
        )

        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=320,
            start_date=now - timedelta(days=3),
            end_date=now,
        )
        Comic.objects.create(
            user=self.user,
            item=comic_item,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=now - timedelta(days=2),
            end_date=now,
        )
        Manga.objects.create(
            user=self.user,
            item=manga_item,
            status=Status.IN_PROGRESS.value,
            progress=85,
            start_date=now - timedelta(days=1),
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        book_genres = [genre["name"] for genre in response.context["book_consumption"]["top_genres"]]
        comic_genres = [genre["name"] for genre in response.context["comic_consumption"]["top_genres"]]
        manga_genres = [genre["name"] for genre in response.context["manga_consumption"]["top_genres"]]

        self.assertIn("Fantasy", book_genres)
        self.assertIn("Adventure", book_genres)
        self.assertIn("Sci-Fi", comic_genres)
        self.assertIn("Shonen", manga_genres)
        response_body = response.content.decode()
        self.assertRegex(
            response_body,
            r"·\s*\d+\s+books?",
        )
        self.assertRegex(
            response_body,
            r"·\s*\d+\s+comics?",
        )
        self.assertRegex(
            response_body,
            r"·\s*\d+\s+manga\b",
        )

    def test_updating_reading_scores_refreshes_top_rated_cards(self):
        """Updating reading scores should invalidate day caches used by top-rated cards."""
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        book_item = Item.objects.create(
            media_id="book-rated-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Rated Book",
            image="http://example.com/rated-book.jpg",
            genres=["Fantasy"],
        )
        comic_item = Item.objects.create(
            media_id="comic-rated-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Rated Comic",
            image="http://example.com/rated-comic.jpg",
            genres=["Sci-Fi"],
        )
        manga_item = Item.objects.create(
            media_id="manga-rated-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Rated Manga",
            image="http://example.com/rated-manga.jpg",
            genres=["Shonen"],
        )

        book_entry = Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=180,
            start_date=now - timedelta(days=125),
            end_date=now - timedelta(days=120),
            score=None,
        )
        comic_entry = Comic.objects.create(
            user=self.user,
            item=comic_item,
            status=Status.IN_PROGRESS.value,
            progress=75,
            start_date=now - timedelta(days=115),
            end_date=now - timedelta(days=110),
            score=None,
        )
        manga_entry = Manga.objects.create(
            user=self.user,
            item=manga_item,
            status=Status.IN_PROGRESS.value,
            progress=95,
            start_date=now - timedelta(days=105),
            end_date=now - timedelta(days=100),
            score=None,
        )

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        stale_response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(stale_response.context["top_rated_book"], [])
        self.assertEqual(stale_response.context["top_rated_comic"], [])
        self.assertEqual(stale_response.context["top_rated_manga"], [])

        self.assertEqual(
            self.client.post(
                reverse("update_media_score", args=[MediaTypes.BOOK.value, book_entry.id]),
                {"score": "8"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse("update_media_score", args=[MediaTypes.COMIC.value, comic_entry.id]),
                {"score": "7"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse("update_media_score", args=[MediaTypes.MANGA.value, manga_entry.id]),
                {"score": "9"},
            ).status_code,
            200,
        )

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        book_titles = [media.item.title for media in refreshed_response.context["top_rated_book"]]
        comic_titles = [media.item.title for media in refreshed_response.context["top_rated_comic"]]
        manga_titles = [media.item.title for media in refreshed_response.context["top_rated_manga"]]

        self.assertIn("Rated Book", book_titles)
        self.assertIn("Rated Comic", comic_titles)
        self.assertIn("Rated Manga", manga_titles)

    def test_refresh_statistics_cache_repairs_stale_reading_score_days(self):
        """All-time refresh should rebuild stale reading score days missed by older invalidation logic."""
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        stale_cases = [
            {
                "cache_key": MediaTypes.BOOK.value,
                "model": Book,
                "media_type": MediaTypes.BOOK.value,
                "media_id": "book-stale-score-1",
                "title": "Stale Score Book",
                "image": "http://example.com/stale-score-book.jpg",
                "genres": ["Fantasy"],
                "progress": 250,
                "offset_days": 120,
                "updated_score": 8,
            },
            {
                "cache_key": MediaTypes.COMIC.value,
                "model": Comic,
                "media_type": MediaTypes.COMIC.value,
                "media_id": "comic-stale-score-1",
                "title": "Stale Score Comic",
                "image": "http://example.com/stale-score-comic.jpg",
                "genres": ["Sci-Fi"],
                "progress": 120,
                "offset_days": 121,
                "updated_score": 9,
            },
            {
                "cache_key": MediaTypes.MANGA.value,
                "model": Manga,
                "media_type": MediaTypes.MANGA.value,
                "media_id": "manga-stale-score-1",
                "title": "Stale Score Manga",
                "image": "http://example.com/stale-score-manga.jpg",
                "genres": ["Shonen"],
                "progress": 85,
                "offset_days": 122,
                "updated_score": 10,
            },
        ]
        created_entries = []
        for case in stale_cases:
            item = Item.objects.create(
                media_id=case["media_id"],
                source=Sources.MANUAL.value,
                media_type=case["media_type"],
                title=case["title"],
                image=case["image"],
                genres=case["genres"],
            )
            entry = case["model"].objects.create(
                user=self.user,
                item=item,
                status=Status.COMPLETED.value,
                progress=case["progress"],
                start_date=None,
                end_date=now - timedelta(days=case["offset_days"]),
                score=None,
            )
            created_entries.append((case, item, entry))

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        for case, item, entry in created_entries:
            stale_day_key = history_cache.history_day_key(entry.end_date)
            stale_cache_key = statistics_cache._day_cache_key(self.user.id, stale_day_key)
            stale_day_payload = cache.get(stale_cache_key)
            stale_item_payload = stale_day_payload["items"][case["cache_key"]][str(item.id)]
            self.assertIsNone(stale_item_payload["score"])

        # Simulate legacy score updates that didn't invalidate day caches.
        for case, _item, entry in created_entries:
            case["model"].objects.filter(id=entry.id).update(score=case["updated_score"])

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        book_titles = [media.item.title for media in refreshed_response.context["top_rated_book"]]
        comic_titles = [media.item.title for media in refreshed_response.context["top_rated_comic"]]
        manga_titles = [media.item.title for media in refreshed_response.context["top_rated_manga"]]

        self.assertIn("Stale Score Book", book_titles)
        self.assertIn("Stale Score Comic", comic_titles)
        self.assertIn("Stale Score Manga", manga_titles)

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_returns_empty_reading_top_genres_when_items_have_no_genres(self, mock_get_metadata):
        """Reading top genres should be empty when source items have no genre metadata."""
        mock_get_metadata.return_value = {"max_progress": 2000}
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        book_item = Item.objects.create(
            media_id="book-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Without Genre",
            image="http://example.com/book-no-genre.jpg",
            genres=[],
        )
        comic_item = Item.objects.create(
            media_id="comic-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Comic Without Genre",
            image="http://example.com/comic-no-genre.jpg",
            genres=[],
        )
        manga_item = Item.objects.create(
            media_id="manga-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga Without Genre",
            image="http://example.com/manga-no-genre.jpg",
            genres=[],
        )

        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=300,
            start_date=now - timedelta(days=3),
            end_date=now,
        )
        Comic.objects.create(
            user=self.user,
            item=comic_item,
            status=Status.IN_PROGRESS.value,
            progress=110,
            start_date=now - timedelta(days=2),
            end_date=now,
        )
        Manga.objects.create(
            user=self.user,
            item=manga_item,
            status=Status.IN_PROGRESS.value,
            progress=90,
            start_date=now - timedelta(days=1),
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["book_consumption"]["top_genres"], [])
        self.assertEqual(response.context["comic_consumption"]["top_genres"], [])
        self.assertEqual(response.context["manga_consumption"]["top_genres"], [])

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.tasks.enqueue_genre_backfill_items")
    def test_build_history_day_enqueues_genre_backfill_for_reading_entries_with_missing_genres(
        self,
        mock_enqueue_genre_backfill_items,
        _mock_get_media_metadata,
    ):
        """Reading entries missing genres should enqueue genre backfill item IDs."""
        _mock_get_media_metadata.return_value = {"max_progress": 120}
        cache.clear()
        now = timezone.now()
        book_item = Item.objects.create(
            media_id="book-missing-genre",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Missing Genre",
            image="http://example.com/book-missing-genre.jpg",
            genres=[],
        )
        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=now - timedelta(days=1),
            end_date=now,
        )

        statistics_cache.build_stats_for_day(self.user.id, now.date())

        self.assertIn(
            call([book_item.id]),
            mock_enqueue_genre_backfill_items.mock_calls,
        )

    @patch("app.statistics_cache._aggregate_top_talent")
    def test_statistics_all_time_uses_aware_boundaries_for_top_talent(self, mock_top_talent):
        """All-time aggregation should pass aware datetime boundaries to top talent."""
        mock_top_talent.return_value = {
            "sort_by": "plays",
            "top_actors": [],
            "top_actresses": [],
            "top_directors": [],
            "top_writers": [],
            "top_studios": [],
        }

        day_list = [
            timezone.localdate() - timedelta(days=7),
            timezone.localdate(),
        ]
        statistics_cache._aggregate_statistics_from_days(
            self.user,
            day_list,
            start_date=None,
            end_date=None,
            build_missing=False,
        )

        self.assertTrue(mock_top_talent.called)
        _, start_date, end_date = mock_top_talent.call_args.args[:3]
        self.assertTrue(timezone.is_aware(start_date))
        self.assertTrue(timezone.is_aware(end_date))

    def test_statistics_view_includes_top_talent_sections(self):
        """Top cast/crew and studio sections should be present in context."""
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Talent Movie",
            image="http://example.com/talent.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )

        actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="100",
            name="Actor Person",
            gender=PersonGender.MALE.value,
        )
        actress = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="101",
            name="Actress Person",
            gender=PersonGender.FEMALE.value,
        )
        director = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="102",
            name="Director Person",
            gender=PersonGender.UNKNOWN.value,
        )
        writer = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="103",
            name="Writer Person",
            gender=PersonGender.UNKNOWN.value,
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="500",
            name="Studio Person",
        )

        ItemPersonCredit.objects.create(
            item=item,
            person=actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=actress,
            role_type=CreditRoleType.CAST.value,
            role="Co-Lead",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=director,
            role_type=CreditRoleType.CREW.value,
            role="Director",
            department="Directing",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=writer,
            role_type=CreditRoleType.CREW.value,
            role="Writer",
            department="Writing",
        )
        ItemStudioCredit.objects.create(item=item, studio=studio)

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        top_talent = response.context.get("top_talent", {})
        self.assertTrue(any(entry["name"] == "Actor Person" for entry in top_talent.get("top_actors", [])))
        self.assertTrue(any(entry["name"] == "Actress Person" for entry in top_talent.get("top_actresses", [])))
        self.assertTrue(any(entry["name"] == "Director Person" for entry in top_talent.get("top_directors", [])))
        self.assertTrue(any(entry["name"] == "Writer Person" for entry in top_talent.get("top_writers", [])))
        self.assertTrue(any(entry["name"] == "Studio Person" for entry in top_talent.get("top_studios", [])))
        actor_entry = next(entry for entry in top_talent.get("top_actors", []) if entry["name"] == "Actor Person")
        studio_entry = next(entry for entry in top_talent.get("top_studios", []) if entry["name"] == "Studio Person")
        self.assertEqual(actor_entry.get("unique_movies"), 1)
        self.assertEqual(actor_entry.get("unique_shows"), 0)
        self.assertEqual(studio_entry.get("unique_movies"), 1)
        self.assertEqual(studio_entry.get("unique_shows"), 0)

    def test_statistics_top_talent_sort_modes_affect_rank_and_subtitle(self):
        """Top talent cards should sort and display subtitle metric by preference."""
        watched_at = timezone.now()
        titles_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="201",
            name="Titles Leader",
            gender=PersonGender.MALE.value,
        )
        plays_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="202",
            name="Plays Leader",
            gender=PersonGender.MALE.value,
        )

        titles_movie_1 = Item.objects.create(
            media_id="2001",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Titles Movie One",
            runtime_minutes=100,
            image="http://example.com/titles1.jpg",
        )
        titles_movie_2 = Item.objects.create(
            media_id="2002",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Titles Movie Two",
            runtime_minutes=100,
            image="http://example.com/titles2.jpg",
        )
        plays_movie = Item.objects.create(
            media_id="2003",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Plays Movie",
            runtime_minutes=30,
            image="http://example.com/plays.jpg",
        )

        ItemPersonCredit.objects.create(
            item=titles_movie_1,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=titles_movie_2,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=plays_movie,
            person=plays_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )

        Movie.objects.create(
            item=titles_movie_1,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )
        Movie.objects.create(
            item=titles_movie_2,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at + timedelta(minutes=1),
            end_date=watched_at + timedelta(minutes=1),
        )
        for offset in range(3):
            Movie.objects.create(
                item=plays_movie,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                start_date=watched_at + timedelta(minutes=10 + offset),
                end_date=watched_at + timedelta(minutes=10 + offset),
            )

        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Plays Leader",
        )
        self.assertContains(response, "3 Plays")

        self.user.top_talent_sort_by = "time"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertContains(response, "3h 20min")

        self.user.top_talent_sort_by = "titles"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertContains(response, "2 Titles")

    def test_statistics_top_talent_precomputes_all_sort_modes(self):
        """Top talent payload should include rankings precomputed for plays, time, and titles."""
        watched_at = timezone.now()
        plays_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="301",
            name="Plays Leader",
            gender=PersonGender.MALE.value,
        )
        titles_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="302",
            name="Titles Leader",
            gender=PersonGender.MALE.value,
        )

        plays_item = Item.objects.create(
            media_id="3001",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Short Movie",
            runtime_minutes=30,
            image="http://example.com/short.jpg",
        )
        titles_item_1 = Item.objects.create(
            media_id="3002",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Long Movie One",
            runtime_minutes=60,
            image="http://example.com/long1.jpg",
        )
        titles_item_2 = Item.objects.create(
            media_id="3003",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Long Movie Two",
            runtime_minutes=60,
            image="http://example.com/long2.jpg",
        )

        ItemPersonCredit.objects.create(
            item=plays_item,
            person=plays_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=titles_item_1,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=titles_item_2,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )

        for offset in range(3):
            Movie.objects.create(
                item=plays_item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                start_date=watched_at + timedelta(minutes=offset),
                end_date=watched_at + timedelta(minutes=offset),
            )
        for item in (titles_item_1, titles_item_2):
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                start_date=watched_at + timedelta(minutes=10),
                end_date=watched_at + timedelta(minutes=10),
            )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_talent = response.context["top_talent"]
        self.assertIn("by_sort", top_talent)
        self.assertEqual(
            top_talent["by_sort"]["plays"]["top_actors"][0]["name"],
            "Plays Leader",
        )
        self.assertEqual(
            top_talent["by_sort"]["time"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertEqual(
            top_talent["by_sort"]["titles"]["top_actors"][0]["name"],
            "Titles Leader",
        )

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_updates_preference_without_cache_rebuild(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Statistics sort autosave should persist preference without forcing cache rebuild."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "time", "range_name": "All Time"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["sort_by"], "time")

        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "time")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_rejects_invalid_value(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Statistics sort autosave should reject invalid values."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "invalid_sort", "range_name": "All Time"},
        )

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "plays")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_custom_range_does_not_schedule_refresh(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Autosave with a custom range should still avoid cache rebuild side effects."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "titles", "range_name": "Custom Range"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["sort_by"], "titles")

        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "titles")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    @patch("app.views.statistics_cache.range_needs_top_talent_upgrade")
    def test_update_top_talent_sort_legacy_cache_triggers_upgrade_and_reload(
        self,
        mock_range_needs_upgrade,
        mock_invalidate,
        mock_refresh,
    ):
        """Legacy cached top_talent payload should be upgraded and prompt reload."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])
        mock_range_needs_upgrade.return_value = True

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "time", "range_name": "All Time"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertTrue(payload["requires_reload"])
        self.assertEqual(payload["sort_by"], "time")
        mock_range_needs_upgrade.assert_called_once_with(self.user.id, "All Time")
        mock_invalidate.assert_called_once_with(self.user.id, "All Time")
        mock_refresh.assert_called_once_with(self.user.id, "All Time")

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_uses_episode_credits_with_show_fallback(self, mock_enqueue):
        """Episode plays should use episode credits when present, otherwise fallback to show credits."""
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Fallback Show",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        season_item, _ = Item.objects.get_or_create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "Fallback Show",
                "image": "http://example.com/season.jpg",
            },
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        episode_item_one, _ = Item.objects.get_or_create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={
                "title": "Fallback Show",
                "image": "http://example.com/e1.jpg",
                "runtime_minutes": 50,
            },
        )
        episode_item_two, _ = Item.objects.get_or_create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            defaults={
                "title": "Fallback Show",
                "image": "http://example.com/e2.jpg",
                "runtime_minutes": 50,
            },
        )

        Episode.objects.bulk_create(
            [
                Episode(
                    item=episode_item_one,
                    related_season=season,
                    end_date=watched_at,
                ),
                Episode(
                    item=episode_item_two,
                    related_season=season,
                    end_date=watched_at + timedelta(minutes=1),
                ),
            ],
        )

        show_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="301",
            name="Show Fallback Actor",
            gender=PersonGender.MALE.value,
        )
        episode_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="302",
            name="Episode Specific Actor",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=show_item,
            person=show_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=episode_item_one,
            person=episode_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        MetadataBackfillState.objects.create(
            item=episode_item_one,
            field=MetadataBackfillField.CREDITS,
            last_success_at=timezone.now(),
            strategy_version=CREDITS_BACKFILL_VERSION,
        )

        mock_enqueue.reset_mock()
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_called_once()
        enqueue_args, enqueue_kwargs = mock_enqueue.call_args
        scheduled_ids = sorted(enqueue_args[0])
        self.assertIn(episode_item_two.id, scheduled_ids)
        self.assertEqual(enqueue_kwargs, {"countdown": 3})
        top_actors = response.context["top_talent"]["top_actors"]
        by_name = {entry["name"]: entry for entry in top_actors}
        self.assertIn("Episode Specific Actor", by_name)
        self.assertIn("Show Fallback Actor", by_name)
        self.assertEqual(by_name["Episode Specific Actor"]["plays"], 1)
        self.assertEqual(by_name["Show Fallback Actor"]["plays"], 1)

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_does_not_use_show_fallback_when_episode_has_people(self, _mock_enqueue):
        """Episode plays should not use show-level fallback when episode credits exist."""
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="4100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="No Category Fallback Show",
            image="http://example.com/no-fallback-show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        season_item, _ = Item.objects.get_or_create(
            media_id="4100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "No Category Fallback Show",
                "image": "http://example.com/no-fallback-season.jpg",
            },
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        episode_item, _ = Item.objects.get_or_create(
            media_id="4100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={
                "title": "No Category Fallback Show",
                "image": "http://example.com/no-fallback-e1.jpg",
                "runtime_minutes": 42,
            },
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=watched_at,
        )

        show_actress = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="4110",
            name="Show-Level Actress",
            gender=PersonGender.FEMALE.value,
        )
        episode_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="4111",
            name="Episode-Level Actor",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=show_item,
            person=show_actress,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=episode_item,
            person=episode_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        MetadataBackfillState.objects.create(
            item=episode_item,
            field=MetadataBackfillField.CREDITS,
            last_success_at=timezone.now(),
            strategy_version=CREDITS_BACKFILL_VERSION,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_talent = response.context["top_talent"]
        actress_names = {entry["name"] for entry in top_talent["top_actresses"]}
        actor_names = {entry["name"] for entry in top_talent["top_actors"]}
        self.assertNotIn("Show-Level Actress", actress_names)
        self.assertIn("Episode-Level Actor", actor_names)

    @patch("app.providers.services.get_media_metadata")
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_view_queues_credit_backfill_for_missing_tmdb_item(self, mock_enqueue, mock_get_metadata):
        """Statistics should queue credit backfill for played TMDB items missing credits."""
        mock_get_metadata.return_value = {"max_progress": 1}
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="42",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Missing Credits Movie",
            image="http://example.com/missing.jpg",
            runtime_minutes=120,
            genres=["Drama"],
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )
        mock_enqueue.reset_mock()

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_called_once_with([item.id], countdown=3)

    @patch("app.providers.services.get_media_metadata")
    @patch("app.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_refresh_statistics_schedules_credit_backfill_once_per_refresh_cycle(
        self,
        mock_enqueue,
        _mock_schedule_all_ranges_refresh,
        mock_get_metadata,
    ):
        """Day refresh should schedule missing credits without duplicate enqueue in top-talent aggregate."""
        mock_get_metadata.return_value = {"max_progress": 1}
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="9042",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Refresh Missing Credits",
            image="http://example.com/missing-refresh.jpg",
            runtime_minutes=120,
            genres=["Drama"],
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )
        mock_enqueue.reset_mock()
        mock_enqueue.return_value = 1

        statistics_cache.invalidate_statistics_cache(self.user.id)
        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        mock_enqueue.assert_called_once_with([item.id], countdown=3)

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_build_stats_for_day_backfill_payload_ignores_non_int_scheduled_count(self, mock_enqueue):
        """Cache payload should keep scheduled_credits numeric when enqueue helper is mocked."""
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="9043",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Day Missing Credits",
            image="http://example.com/missing-day.jpg",
            runtime_minutes=100,
            genres=["Drama"],
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )
        mock_enqueue.return_value = object()

        day_stats = statistics_cache.build_stats_for_day(self.user.id, watched_at.date())

        self.assertEqual(day_stats["backfill"]["missing_credits"], 1)
        self.assertEqual(day_stats["backfill"]["scheduled_credits"], 0)
