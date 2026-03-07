from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app import tasks
from app.models import (
    CREDITS_BACKFILL_VERSION,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Person,
    PersonGender,
    Sources,
    Studio,
)

User = get_user_model()


class MetadataBackfillTaskTests(TestCase):
    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_updates_release_datetime_for_existing_items(self, mock_get_media_metadata):
        old_fetched_at = timezone.now() - timedelta(days=30)
        item = Item.objects.create(
            media_id="262712",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Ananta",
            image="https://example.com/image.jpg",
            genres=["Action"],
            metadata_fetched_at=old_fetched_at,
        )
        cache_key = f"{Sources.IGDB.value}_{MediaTypes.GAME.value}_{item.media_id}"
        cache.set(cache_key, {"details": {"release_date": None}}, timeout=300)

        mock_get_media_metadata.return_value = {
            "details": {
                "release_date": "2002-11-15",
            },
        }

        result = tasks.backfill_item_metadata_task(batch_size=10)

        item.refresh_from_db()

        self.assertEqual(item.release_datetime.date(), date(2002, 11, 15))
        self.assertGreater(item.metadata_fetched_at, old_fetched_at)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["release_updated_count"], 1)
        self.assertEqual(result["error_count"], 0)
        self.assertIsNone(cache.get(cache_key))
        mock_get_media_metadata.assert_any_call(
            MediaTypes.GAME.value,
            item.media_id,
            item.source,
        )

    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_initial_metadata_includes_release_datetime(self, mock_get_media_metadata):
        item = Item.objects.create(
            media_id="9999",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
            image="https://example.com/book.jpg",
        )
        self.assertIsNone(item.metadata_fetched_at)

        mock_get_media_metadata.return_value = {
            "details": {
                "publish_date": "1999-03-31",
                "country": "US",
            },
        }

        result = tasks.backfill_item_metadata_task(batch_size=10)

        item.refresh_from_db()

        self.assertEqual(item.release_datetime.date(), date(1999, 3, 31))
        self.assertEqual(item.country, "US")
        self.assertIsNotNone(item.metadata_fetched_at)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["release_updated_count"], 1)
        self.assertEqual(result["error_count"], 0)

    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_updates_movie_provider_recommendation_metadata(self, mock_get_media_metadata):
        item = Item.objects.create(
            media_id="501",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="https://example.com/movie.jpg",
        )

        mock_get_media_metadata.return_value = {
            "provider_popularity": 77.5,
            "provider_rating": 7.7,
            "provider_rating_count": 1200,
            "provider_keywords": ["Whodunit", "Holiday"],
            "provider_certification": "PG",
            "provider_collection_id": "321",
            "provider_collection_name": "Mystery Collection",
            "details": {
                "country": "US",
            },
        }

        result = tasks.backfill_item_metadata_task(batch_size=10)

        item.refresh_from_db()
        self.assertEqual(item.provider_popularity, 77.5)
        self.assertEqual(item.provider_rating, 7.7)
        self.assertEqual(item.provider_rating_count, 1200)
        self.assertEqual(item.provider_keywords, ["Whodunit", "Holiday"])
        self.assertEqual(item.provider_certification, "PG")
        self.assertEqual(item.provider_collection_id, "321")
        self.assertEqual(item.provider_collection_name, "Mystery Collection")
        self.assertEqual(result["success_count"], 1)

    def test_discover_movie_metadata_queryset_includes_existing_tmdb_movies_until_version_marked(self):
        item = Item.objects.create(
            media_id="601",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Existing Movie",
            image="https://example.com/movie.jpg",
            metadata_fetched_at=timezone.now() - timedelta(days=30),
        )

        candidate_ids = set(
            tasks._discover_movie_metadata_items_queryset().values_list("id", flat=True),
        )
        self.assertIn(item.id, candidate_ids)

        MetadataBackfillState.objects.create(
            item=item,
            field=MetadataBackfillField.DISCOVER,
            strategy_version=tasks.DISCOVER_MOVIE_METADATA_BACKFILL_VERSION,
            last_success_at=timezone.now(),
        )

        candidate_ids = set(
            tasks._discover_movie_metadata_items_queryset().values_list("id", flat=True),
        )
        self.assertNotIn(item.id, candidate_ids)

    @patch("app.tasks.refresh_discover_tab_cache.apply_async")
    @patch("app.tasks.refresh_discover_profiles.apply_async")
    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_existing_tmdb_movies_schedules_discover_refresh(
        self,
        mock_get_media_metadata,
        mock_refresh_profiles,
        mock_refresh_tab_cache,
    ):
        user = User.objects.create_user(username="discover-user", password="pw")
        old_fetched_at = timezone.now() - timedelta(days=30)
        item = Item.objects.create(
            media_id="701",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Discover Movie",
            image="https://example.com/discover-movie.jpg",
            metadata_fetched_at=old_fetched_at,
            release_datetime=timezone.now() - timedelta(days=365),
        )
        from app.models import Movie

        with patch(
            "app.models.providers.services.get_media_metadata",
            return_value={"max_progress": 1},
        ):
            Movie.objects.create(
                item=item,
                user=user,
                status="Completed",
            )

        mock_get_media_metadata.return_value = {
            "provider_popularity": 88.0,
            "provider_rating": 8.1,
            "provider_rating_count": 1400,
            "provider_keywords": ["Whodunit"],
            "provider_certification": "PG",
            "provider_collection_id": "88",
            "provider_collection_name": "Mystery Collection",
            "details": {"country": "US"},
        }

        result = tasks.backfill_item_metadata_task(batch_size=1)

        item.refresh_from_db()
        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.DISCOVER,
        )

        self.assertEqual(item.provider_keywords, ["Whodunit"])
        self.assertEqual(item.provider_popularity, 88.0)
        self.assertEqual(item.provider_rating, 8.1)
        self.assertEqual(item.provider_rating_count, 1400)
        self.assertEqual(item.provider_certification, "PG")
        self.assertEqual(state.strategy_version, tasks.DISCOVER_MOVIE_METADATA_BACKFILL_VERSION)
        self.assertIsNotNone(state.last_success_at)
        self.assertEqual(result["success_count"], 1)
        self.assertIn("remaining_discover_movie_metadata", result)

        mock_refresh_profiles.assert_called_once()
        self.assertEqual(mock_refresh_tab_cache.call_count, 2)

    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_prioritizes_never_fetched_items(self, mock_get_media_metadata):
        never_fetched = Item.objects.create(
            media_id="100",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Never Fetched",
            image="https://example.com/a.jpg",
        )
        old_fetched_at = timezone.now() - timedelta(days=10)
        release_only = Item.objects.create(
            media_id="200",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Release Only",
            image="https://example.com/b.jpg",
            genres=["Action"],
            metadata_fetched_at=old_fetched_at,
        )

        mock_get_media_metadata.return_value = {
            "details": {"release_date": "2020-01-01"},
        }

        tasks.backfill_item_metadata_task(batch_size=1)

        never_fetched.refresh_from_db()
        release_only.refresh_from_db()

        self.assertIsNotNone(never_fetched.metadata_fetched_at)
        self.assertIsNone(release_only.release_datetime)
        self.assertEqual(release_only.metadata_fetched_at, old_fetched_at)
        mock_get_media_metadata.assert_any_call(
            MediaTypes.BOOK.value,
            never_fetched.media_id,
            never_fetched.source,
        )

    def test_genre_backfill_queryset_includes_reading_media_types(self):
        book = Item.objects.create(
            media_id="book-no-genre",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
            image="https://example.com/book.jpg",
            genres=[],
        )
        comic = Item.objects.create(
            media_id="comic-no-genre",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
            title="Comic",
            image="https://example.com/comic.jpg",
            genres=[],
        )
        manga = Item.objects.create(
            media_id="manga-no-genre",
            source=Sources.MANGAUPDATES.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga",
            image="https://example.com/manga.jpg",
            genres=[],
        )

        queued_ids = set(tasks._genre_items_queryset().values_list("id", flat=True))

        self.assertIn(book.id, queued_ids)
        self.assertIn(comic.id, queued_ids)
        self.assertIn(manga.id, queued_ids)

    def test_next_credits_backfill_item_ids_filters_complete_items(self):
        missing_movie = Item.objects.create(
            media_id="missing-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Needs Credits",
            image="https://example.com/missing-movie.jpg",
            genres=["Drama"],
            runtime_minutes=100,
        )
        complete_movie = Item.objects.create(
            media_id="complete-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Complete Credits",
            image="https://example.com/complete-movie.jpg",
            genres=["Drama"],
            runtime_minutes=99,
        )
        episode_missing_strategy = Item.objects.create(
            media_id="episode-credits",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode",
            image="https://example.com/episode.jpg",
        )
        person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="person-1",
            name="Person One",
            gender=PersonGender.UNKNOWN.value,
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="studio-1",
            name="Studio One",
        )
        ItemPersonCredit.objects.create(
            item=complete_movie,
            person=person,
            role_type="cast",
            role="Lead",
        )
        ItemStudioCredit.objects.create(
            item=complete_movie,
            studio=studio,
        )
        ItemPersonCredit.objects.create(
            item=episode_missing_strategy,
            person=person,
            role_type="cast",
            role="Lead",
        )
        MetadataBackfillState.objects.create(
            item=episode_missing_strategy,
            field=MetadataBackfillField.CREDITS,
            strategy_version=max(CREDITS_BACKFILL_VERSION - 1, 1),
            last_success_at=timezone.now(),
        )

        candidate_ids = tasks._next_credits_backfill_item_ids(batch_size=5, scan_multiplier=5)

        self.assertIn(missing_movie.id, candidate_ids)
        self.assertIn(episode_missing_strategy.id, candidate_ids)
        self.assertNotIn(complete_movie.id, candidate_ids)

    @patch("app.tasks.enqueue_credits_backfill_items", return_value=4)
    @patch("app.tasks.enqueue_episode_runtime_backfill", return_value=3)
    @patch("app.tasks.enqueue_runtime_backfill_items", return_value=2)
    @patch("app.tasks.enqueue_genre_backfill_items", return_value=1)
    @patch("app.tasks._next_credits_backfill_item_ids")
    def test_nightly_metadata_quality_backfill_queues_all_dimensions(
        self,
        mock_next_credits,
        mock_enqueue_genres,
        mock_enqueue_runtime,
        mock_enqueue_episode_runtime,
        mock_enqueue_credits,
    ):
        genre_item = Item.objects.create(
            media_id="game-genre",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Genre Game",
            image="https://example.com/game.jpg",
            genres=[],
        )
        runtime_item = Item.objects.create(
            media_id="anime-runtime",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Runtime Anime",
            image="https://example.com/anime.jpg",
            genres=["Action"],
            runtime_minutes=None,
        )
        episode_item = Item.objects.create(
            media_id="tv-episode",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode Runtime",
            image="https://example.com/episode-runtime.jpg",
            runtime_minutes=None,
        )
        credits_item = Item.objects.create(
            media_id="credits-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Credits Movie",
            image="https://example.com/credits-movie.jpg",
            genres=["Mystery"],
            runtime_minutes=102,
        )
        mock_next_credits.return_value = [credits_item.id]

        result = tasks.nightly_metadata_quality_backfill_task(
            genre_batch_size=20,
            runtime_batch_size=20,
            episode_season_batch_size=20,
            credits_batch_size=20,
            credits_scan_multiplier=3,
        )

        mock_next_credits.assert_called_once_with(20, scan_multiplier=3)
        mock_enqueue_genres.assert_called_once_with(
            [genre_item.id],
            countdown=tasks.NIGHTLY_METADATA_QUALITY_GENRE_COUNTDOWN,
        )
        mock_enqueue_runtime.assert_called_once_with(
            [runtime_item.id],
            countdown=tasks.NIGHTLY_METADATA_QUALITY_RUNTIME_COUNTDOWN,
        )
        mock_enqueue_episode_runtime.assert_called_once_with(
            [(episode_item.media_id, episode_item.source, episode_item.season_number)],
            countdown=tasks.NIGHTLY_METADATA_QUALITY_EPISODE_COUNTDOWN,
        )
        mock_enqueue_credits.assert_called_once_with(
            [credits_item.id],
            countdown=tasks.NIGHTLY_METADATA_QUALITY_CREDITS_COUNTDOWN,
        )
        self.assertEqual(result["selected"]["genres"], 1)
        self.assertEqual(result["selected"]["runtime"], 1)
        self.assertEqual(result["selected"]["episode_seasons"], 1)
        self.assertEqual(result["selected"]["credits"], 1)
        self.assertEqual(result["queued"]["genres"], 1)
        self.assertEqual(result["queued"]["runtime"], 2)
        self.assertEqual(result["queued"]["episode_seasons"], 3)
        self.assertEqual(result["queued"]["credits"], 4)
