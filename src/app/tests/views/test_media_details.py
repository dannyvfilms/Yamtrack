from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import statistics_cache
from app.models import (
    Book,
    CreditRoleType,
    Item,
    ItemPersonCredit,
    MediaTypes,
    Person,
    PodcastEpisode,
    PodcastShow,
    Sources,
    Status,
)
from integrations.models import PlexAccount


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")

        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }

        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 1,
                "name": "Episode 1",
                "air_date": "2023-01-01",
                "watched": False,
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)
        self.assertContains(
            response,
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.EPISODE.value, "1668", 1, 1],
            ),
        )

        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )

    @patch("integrations.tasks.fetch_collection_metadata_for_item.delay")
    @patch("app.providers.services.get_media_metadata")
    def test_game_details_skips_collection_autofetch(
        self,
        mock_get_metadata,
        mock_fetch_delay,
    ):
        """Game details should not trigger collection auto-fetch."""
        mock_get_metadata.return_value = {
            "media_id": "game-123",
            "title": "Test Game",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/game.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        Item.objects.create(
            media_id="game-123",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )

        PlexAccount.objects.create(
            user=self.user,
            plex_token="plex-token",
            plex_username="plex-user",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "game-123",
                    "title": "test-game",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["fetching_collection_data"])
        self.assertIsNone(response.context["item_id_for_polling"])
        mock_fetch_delay.assert_not_called()

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_cast_and_crew_links(self, mock_get_metadata):
        """Movie details should render cast/crew links to person pages."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/movie/238",
            "image": "http://example.com/image.jpg",
            "synopsis": "Test synopsis",
            "details": {"format": "Movie"},
            "cast": [
                {
                    "person_id": "10",
                    "name": "John Actor",
                    "role": "Hero",
                },
            ],
            "crew": [
                {
                    "person_id": "11",
                    "name": "Jane Director",
                    "role": "Director",
                    "department": "Directing",
                },
            ],
            "studios_full": [
                {
                    "studio_id": "20",
                    "name": "Studio One",
                },
            ],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John Actor")
        self.assertContains(response, "Jane Director")
        self.assertContains(response, "Studio One")
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.TMDB.value,
                    "person_id": "10",
                    "name": "john-actor",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_backfills_author_credits_and_renders_links(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "OL123M",
            "title": "Linked Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.OPENLIBRARY.value,
            "source_url": "https://openlibrary.org/books/OL123M",
            "image": "http://example.com/book.jpg",
            "synopsis": "Book synopsis",
            "max_progress": 300,
            "details": {
                "author": ["Open Author"],
                "publish_date": "2000-01-01",
            },
            "authors_full": [
                {
                    "person_id": "OL1A",
                    "name": "Open Author",
                    "image": "http://example.com/author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
            "related": {},
        }

        item = Item.objects.create(
            media_id="OL123M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Linked Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=300,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "OL123M",
                    "title": "linked-book",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        author_person = Person.objects.get(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL1A",
        )
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=author_person,
                role_type=CreditRoleType.AUTHOR.value,
            ).exists(),
        )
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL1A",
                    "name": "open-author",
                },
            ),
        )
        html = response.content.decode()
        self.assertEqual(
            html.count('text-sm font-semibold text-gray-400">AUTHOR</h3>'),
            1,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_uses_authors_full_fallback_without_item(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "72274276213",
            "title": "Metadata Only Manga",
            "media_type": MediaTypes.MANGA.value,
            "source": Sources.MANGAUPDATES.value,
            "source_url": "https://www.mangaupdates.com/series/72274276213",
            "image": "http://example.com/manga.jpg",
            "synopsis": "Manga synopsis",
            "details": {
                "authors": ["Manga Author"],
            },
            "authors_full": [
                {
                    "person_id": "55",
                    "name": "Manga Author",
                    "image": "http://example.com/manga-author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "media_type": MediaTypes.MANGA.value,
                    "media_id": "72274276213",
                    "title": "metadata-only-manga",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ItemPersonCredit.objects.count(), 0)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "person_id": "55",
                    "name": "manga-author",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_refreshes_stale_author_cache_and_renders_links(
        self,
        mock_get_metadata,
    ):
        stale_metadata = {
            "media_id": "OL999M",
            "title": "Cached Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.OPENLIBRARY.value,
            "source_url": "https://openlibrary.org/books/OL999M",
            "image": "http://example.com/book.jpg",
            "synopsis": "Book synopsis",
            "max_progress": 320,
            "details": {
                "author": ["Cached Author"],
                "publish_date": "1999-01-01",
            },
            "related": {},
        }
        refreshed_metadata = {
            **stale_metadata,
            "authors_full": [
                {
                    "person_id": "OL9A",
                    "name": "Cached Author",
                    "image": "http://example.com/author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
        }
        call_count = {"count": 0}

        def _metadata_side_effect(*_args, **_kwargs):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return stale_metadata
            return refreshed_metadata

        mock_get_metadata.side_effect = _metadata_side_effect

        item = Item.objects.create(
            media_id="OL999M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Cached Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=320,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        cache_key = f"{Sources.OPENLIBRARY.value}_{MediaTypes.BOOK.value}_OL999M"
        cache.set(cache_key, stale_metadata)

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "OL999M",
                    "title": "cached-book",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        detail_calls = [
            call
            for call in mock_get_metadata.call_args_list
            if call.args[:3]
            == (
                MediaTypes.BOOK.value,
                "OL999M",
                Sources.OPENLIBRARY.value,
            )
        ]
        self.assertGreaterEqual(len(detail_calls), 2)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL9A",
                    "name": "cached-author",
                },
            ),
        )

        author_person = Person.objects.get(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL9A",
        )
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=author_person,
                role_type=CreditRoleType.AUTHOR.value,
            ).exists(),
        )

    def test_podcast_media_details_renders_for_show_with_no_user_plays(self):
        """Podcast details should render even when episodes have no play history."""
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Dear Hank & John",
            author="Hank and John",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="dhj-episode-1",
            title="Episode One",
            duration=3600,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "media_id": show.podcast_uuid,
                    "title": "dear-hank-john",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dear Hank &amp; John")
        self.assertContains(response, "Episode One")

    def test_podcast_episode_fragment_renders_for_show_with_no_user_plays(self):
        """Podcast episode HTMX fragments should render when no play history exists."""
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Dear Hank & John",
            author="Hank and John",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="dhj-episode-2",
            title="Episode Two",
            duration=1800,
        )

        response = self.client.get(
            reverse("podcast_episodes_api", kwargs={"show_id": show.id}),
            {"format": "html", "page": 1, "page_size": 20},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Episode Two")

    @patch("app.tasks.enqueue_genre_backfill_items", return_value=1)
    def test_media_details_genre_update_refreshes_reading_top_genres(self, _mock_enqueue_genre_backfill_items):
        """Saving reading genres from details should invalidate stale day caches."""
        played_at = timezone.now() - timedelta(days=30)
        item = Item.objects.create(
            media_id="377938",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="The Lord of the Rings",
            image="http://example.com/book.jpg",
            genres=[],
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=900,
            start_date=played_at,
            end_date=played_at,
        )

        statistics_cache.build_stats_for_day(self.user.id, played_at.date())
        stale_stats = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        self.assertEqual(stale_stats["book_consumption"]["top_genres"], [])

        with patch("app.providers.services.get_media_metadata") as mock_get_metadata:
            mock_get_metadata.return_value = {
                "media_id": "377938",
                "title": "The Lord of the Rings",
                "media_type": MediaTypes.BOOK.value,
                "source": Sources.MANUAL.value,
                "image": "http://example.com/book.jpg",
                "max_progress": 1178,
                "genres": ["Fantasy"],
                "details": {"number_of_pages": 1178},
            }
            response = self.client.get(
                reverse(
                    "media_details",
                    kwargs={
                        "source": Sources.MANUAL.value,
                        "media_type": MediaTypes.BOOK.value,
                        "media_id": "377938",
                        "title": "the-lord-of-the-rings",
                    },
                ),
            )
        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertEqual(item.genres, ["Fantasy"])

        statistics_cache.invalidate_statistics_cache(self.user.id, "All Time")
        refreshed_stats = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_genres = [entry["name"] for entry in refreshed_stats["book_consumption"]["top_genres"]]
        self.assertIn("Fantasy", refreshed_genres)

    @patch("app.providers.openlibrary.book")
    def test_audiobookshelf_book_details_does_not_call_openlibrary(
        self,
        mock_openlibrary_book,
    ):
        """Audiobookshelf detail pages should render using local metadata."""
        item = Item.objects.create(
            media_id="f9e2ce45ec9315a7c54c",
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            title="The Blade Itself",
            image="https://img.example/blade.jpg",
            runtime_minutes=1320,
            authors=["Joe Abercrombie"],
            format="audiobook",
        )

        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
            progress=60,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.AUDIOBOOKSHELF.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "f9e2ce45ec9315a7c54c",
                    "title": "the-blade-itself",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The Blade Itself")
        mock_openlibrary_book.assert_not_called()

