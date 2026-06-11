import json
import datetime
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Album,
    Artist,
    Game,
    TV,
    Anime,
    Book,
    Comic,
    ComicIssue,
    Episode,
    Item,
    Manga,
    MediaTypes,
    Music,
    Movie,
    Track,
    Season,
    Sources,
    Status,
)


class CreateMedia(TestCase):
    """Test the creation of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @override_settings(MEDIA_ROOT=("create_media"))
    def test_create_anime(self):
        """Test the creation of a TV object."""
        Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "1",
                "source": Sources.MAL.value,
                "media_type": MediaTypes.ANIME.value,
                "status": Status.PLANNING.value,
                "progress": 0,
                "repeats": 0,
            },
        )
        self.assertEqual(
            Anime.objects.filter(item__media_id="1", user=self.user).exists(),
            True,
        )

    @override_settings(MEDIA_ROOT=("create_media"))
    def test_create_tv(self):
        """Test the creation of a TV object through views."""
        Item.objects.create(
            media_id="5895",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
            image="http://example.com/image.jpg",
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "5895",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )
        self.assertEqual(
            TV.objects.filter(item__media_id="5895", user=self.user).exists(),
            True,
        )

    @patch("app.views.services.get_media_metadata")
    def test_create_tv_with_null_runtime_metadata(self, metadata_mock):
        """Creating TV media should handle provider runtime=None values."""
        metadata_mock.return_value = {
            "title": "Clevatess",
            "original_title": "Clevatess",
            "localized_title": "Clevatess",
            "image": "http://example.com/image.jpg",
            "details": {
                "runtime": None,
            },
        }

        self.client.post(
            reverse("media_save"),
            {
                "media_id": "258348",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )

        self.assertEqual(
            TV.objects.filter(item__media_id="258348", user=self.user).exists(),
            True,
        )
        self.assertEqual(
            Item.objects.get(
                media_id="258348",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
            ).runtime,
            "",
        )

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_reading_media_with_null_format_metadata(self, view_metadata_mock, model_metadata_mock):
        """Creating reading media should coerce None format metadata to an empty string."""
        cases = [
            (
                MediaTypes.BOOK.value,
                Sources.HARDCOVER.value,
                "format-none-book",
                Book,
                100,
            ),
            (
                MediaTypes.COMIC.value,
                Sources.COMICVINE.value,
                "format-none-comic",
                Comic,
                10,
            ),
            (
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                "format-none-manga",
                Manga,
                10,
            ),
        ]

        for media_type, source, media_id, model_class, max_progress in cases:
            metadata = {
                "title": f"Formatless {media_type.title()}",
                "original_title": f"Formatless {media_type.title()}",
                "localized_title": f"Formatless {media_type.title()}",
                "image": "http://example.com/image.jpg",
                "max_progress": max_progress,
                "details": {
                    "format": None,
                    "number_of_pages": max_progress if media_type == MediaTypes.BOOK.value else None,
                },
            }
            view_metadata_mock.return_value = metadata
            model_metadata_mock.return_value = metadata

            response = self.client.post(
                reverse("media_save"),
                {
                    "media_id": media_id,
                    "source": source,
                    "media_type": media_type,
                    "status": Status.PLANNING.value,
                    "progress": 0,
                },
            )

            self.assertEqual(response.status_code, 302)
            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            self.assertEqual(item.format, "")
            self.assertTrue(model_class.objects.filter(item=item, user=self.user).exists())

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_comic_persists_authors_from_people_metadata(self, view_metadata_mock, model_metadata_mock):
        """Comic saves should persist provider people names into Item.authors."""
        metadata = {
            "title": "People Writer Comic",
            "original_title": "People Writer Comic",
            "localized_title": "People Writer Comic",
            "image": "http://example.com/comic.jpg",
            "details": {
                "people": ["Writer One", "Writer Two"],
            },
        }
        view_metadata_mock.return_value = metadata
        model_metadata_mock.return_value = metadata

        response = self.client.post(
            reverse("media_save"),
            {
                "media_id": "comic-people-authors",
                "source": Sources.COMICVINE.value,
                "media_type": MediaTypes.COMIC.value,
                "status": Status.PLANNING.value,
                "progress": 0,
            },
        )

        self.assertEqual(response.status_code, 302)
        item = Item.objects.get(
            media_id="comic-people-authors",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
        )
        self.assertEqual(item.authors, ["Writer One", "Writer Two"])
        self.assertTrue(Comic.objects.filter(item=item, user=self.user).exists())

    @patch("app.save_views.ensure_item_metadata")
    def test_create_comic_issue(self, mock_ensure_item_metadata):
        """Comic issue saves should create ComicIssue rows."""
        item = Item.objects.create(
            media_id="114214",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC_ISSUE.value,
            title="The Iron Prometheus",
            image="http://example.com/issue.jpg",
        )
        mock_ensure_item_metadata.return_value = SimpleNamespace(
            item=item,
            artist=None,
            album=None,
            track=None,
            podcast_show=None,
        )

        response = self.client.post(
            reverse("media_save"),
            {
                "media_id": "114214",
                "source": Sources.COMICVINE.value,
                "media_type": MediaTypes.COMIC_ISSUE.value,
                "status": Status.COMPLETED.value,
                "progress": 1,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ComicIssue.objects.filter(item=item, user=self.user).exists())

    @patch("app.views.services.get_media_metadata")
    def test_create_movie_sets_release_datetime_from_metadata(self, metadata_mock):
        metadata_mock.return_value = {
            "title": "The Matrix",
            "original_title": "The Matrix",
            "localized_title": "The Matrix",
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "details": {
                "release_date": "1999-03-31",
            },
        }

        self.client.post(
            reverse("media_save"),
            {
                "media_id": "603",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "status": Status.PLANNING.value,
            },
        )

        item = Item.objects.get(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        self.assertIsNotNone(item.release_datetime)
        self.assertEqual(item.release_datetime.date(), timezone.datetime(1999, 3, 31).date())

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_reading_media_persists_metadata_genres(self, view_metadata_mock, model_metadata_mock):
        metadata_by_media = {
            (MediaTypes.BOOK.value, "book-genre", Sources.OPENLIBRARY.value): {
                "title": "Book Genre",
                "original_title": "Book Genre",
                "localized_title": "Book Genre",
                "image": "http://example.com/book.jpg",
                "max_progress": 320,
                "genres": ["Fantasy", "Adventure"],
                "details": {"number_of_pages": 320},
            },
            (MediaTypes.COMIC.value, "comic-genre", Sources.COMICVINE.value): {
                "title": "Comic Genre",
                "original_title": "Comic Genre",
                "localized_title": "Comic Genre",
                "image": "http://example.com/comic.jpg",
                "max_progress": 50,
                "genres": ["Sci-Fi"],
                "details": {},
            },
            (MediaTypes.MANGA.value, "manga-genre", Sources.MANGAUPDATES.value): {
                "title": "Manga Genre",
                "original_title": "Manga Genre",
                "localized_title": "Manga Genre",
                "image": "http://example.com/manga.jpg",
                "max_progress": 120,
                "genres": ["Shonen"],
                "details": {},
            },
        }

        def _metadata_side_effect(media_type, media_id, source, *_args, **_kwargs):
            return metadata_by_media[(media_type, media_id, source)]

        view_metadata_mock.side_effect = _metadata_side_effect
        model_metadata_mock.side_effect = _metadata_side_effect

        cases = [
            (
                MediaTypes.BOOK.value,
                Sources.OPENLIBRARY.value,
                "book-genre",
                ["Fantasy", "Adventure"],
                Book,
            ),
            (
                MediaTypes.COMIC.value,
                Sources.COMICVINE.value,
                "comic-genre",
                ["Sci-Fi"],
                Comic,
            ),
            (
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                "manga-genre",
                ["Shonen"],
                Manga,
            ),
        ]

        for media_type, source, media_id, expected_genres, model in cases:
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": media_id,
                    "source": source,
                    "media_type": media_type,
                    "status": Status.PLANNING.value,
                    "progress": 0,
                },
            )

            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            self.assertEqual(item.genres, expected_genres)
            self.assertTrue(model.objects.filter(item=item, user=self.user).exists())

    def test_create_season(self):
        """Test the creation of a Season through views."""
        Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "status": Status.PLANNING.value,
            },
        )
        self.assertEqual(
            Season.objects.filter(item__media_id="1668", user=self.user).exists(),
            True,
        )

    def test_create_episodes(self):
        """Test the creation of Episode through views."""
        self.client.post(
            reverse("episode_save"),
            {
                "media_id": "1668",
                "season_number": 1,
                "episode_number": 1,
                "source": Sources.TMDB.value,
                "date": "2023-06-01T00:00",
            },
        )
        self.assertEqual(
            Episode.objects.filter(
                item__media_id="1668",
                related_season__user=self.user,
                item__episode_number=1,
            ).exists(),
            True,
        )

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_episode_htmx_returns_inline_detail_updates(
        self,
        view_metadata_mock,
        model_metadata_mock,
    ):
        """HTMX episode saves should refresh the season card counters in place."""
        tv_metadata = {
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Friends",
            "original_title": "Friends",
            "localized_title": "Friends",
            "image": "http://example.com/friends.jpg",
            "details": {"seasons": 1},
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "image": "http://example.com/friends-s1.jpg",
                    },
                ],
            },
        }
        season_metadata = {
            **tv_metadata,
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "season_title": "Season 1",
            "details": {"episodes": 1},
            "episodes": [
                {
                    "episode_number": 1,
                    "air_date": "2023-06-01",
                    "image": "http://example.com/friends-s1e1.jpg",
                    "name": "The One Where Monica Gets a Roommate",
                },
            ],
        }

        def metadata_side_effect(media_type, media_id, source, season_numbers=None, **_kwargs):
            self.assertEqual(media_id, "1668")
            self.assertEqual(source, Sources.TMDB.value)
            if media_type == "tv_with_seasons":
                self.assertEqual(season_numbers, [1])
                return {
                    **tv_metadata,
                    "season/1": season_metadata,
                }
            if media_type == MediaTypes.SEASON.value:
                self.assertEqual(season_numbers, [1])
                return season_metadata
            if media_type == MediaTypes.TV.value:
                return tv_metadata
            error_message = f"Unexpected metadata request: {media_type}"
            raise AssertionError(error_message)

        view_metadata_mock.side_effect = metadata_side_effect
        model_metadata_mock.side_effect = metadata_side_effect

        create_response = self.client.post(
            f"{reverse('episode_save')}?next=/details/tmdb/tv/1668/friends/season/1",
            {
                "media_id": "1668",
                "season_number": 1,
                "episode_number": 1,
                "source": Sources.TMDB.value,
                "end_date": "2023-06-01",
            },
        )
        self.assertEqual(create_response.status_code, 302)

        response = self.client.post(
            f"{reverse('episode_save')}?next=/details/tmdb/tv/1668/friends/season/1",
            {
                "media_id": "1668",
                "season_number": 1,
                "episode_number": 1,
                "source": Sources.TMDB.value,
                "end_date": "2023-06-02",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="episode-track-button-', html=False)
        self.assertContains(response, 'id="episode-history-', html=False)
        self.assertContains(response, 'hx-swap-oob="true"', html=False)
        self.assertContains(response, "Watched 2 times")
        self.assertContains(response, "season-progress-mobile-", html=False)
        self.assertContains(response, "season-progress-desktop-", html=False)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["closeModal"], {})
        self.assertEqual(trigger["showToast"]["type"], "success")
        self.assertIn("Added watch", trigger["showToast"]["message"])
        self.assertEqual(
            Episode.objects.filter(
                item__media_id="1668",
                related_season__user=self.user,
                item__episode_number=1,
            ).count(),
            2,
        )

    def test_create_song_htmx_returns_inline_detail_updates(self):
        """HTMX song saves should refresh album track state in place."""
        artist = Artist.objects.create(name="The Futureheads")
        album = Album.objects.create(title="The Futuristics", artist=artist)
        track = Track.objects.create(
            album=album,
            title="Track 1",
            track_number=1,
            disc_number=1,
            musicbrainz_recording_id="recording-1",
            duration_ms=180000,
        )

        create_response = self.client.post(
            f"{reverse('song_save')}?next=/details/music/artist/{artist.id}/the-futureheads/album/{album.id}/the-futuristics",
            {
                "recording_id": "recording-1",
                "album_id": album.id,
                "track_id": track.id,
                "end_date": "2023-06-01T00:00",
            },
        )
        self.assertEqual(create_response.status_code, 302)

        response = self.client.post(
            f"{reverse('song_save')}?next=/details/music/artist/{artist.id}/the-futureheads/album/{album.id}/the-futuristics",
            {
                "recording_id": "recording-1",
                "album_id": album.id,
                "track_id": track.id,
                "end_date": "2023-06-02T00:00",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="music-track-button-', html=False)
        self.assertContains(response, 'id="track-history-', html=False)
        self.assertContains(response, 'hx-swap-oob="true"', html=False)
        self.assertContains(response, "Listened 2 times")
        self.assertContains(response, "music-album-activity-subtitle-", html=False)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["closeModal"], {})
        self.assertEqual(trigger["showToast"]["type"], "success")
        self.assertIn("Added listen", trigger["showToast"]["message"])
        self.assertEqual(
            Music.objects.filter(
                item__media_id="recording-1",
                user=self.user,
                track=track,
            ).count(),
            1,
        )
        self.assertEqual(
            Music.objects.get(
                item__media_id="recording-1",
                user=self.user,
                track=track,
            ).history.count(),
            2,
        )

    @patch("app.views.ensure_item_metadata")
    def test_create_game_htmx_returns_activity_subtitle_update(self, ensure_item_metadata_mock):
        """HTMX game saves should refresh the shared activity subtitle in place."""
        item = Item.objects.create(
            media_id="game-123",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Tracked Game",
            image="http://example.com/game.jpg",
            provider_game_lengths={"igdb": {"summary": {"normally_seconds": 8100}}},
            provider_game_lengths_source="igdb",
        )
        ensure_item_metadata_mock.return_value = SimpleNamespace(item=item)

        response = self.client.post(
            f"{reverse('media_save')}?next=/details/igdb/game/123/tracked-game",
            {
                "track_form_id": "track-form-test",
                "media_id": "game-123",
                "source": Sources.IGDB.value,
                "media_type": MediaTypes.GAME.value,
                "status": Status.PLANNING.value,
                "progress": "3h25min",
                "start_date": "2026-04-13T12:00",
                "end_date": "2026-05-12T12:00",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="detail-activity-subtitle-slot-game-game-123-igdb"', html=False)
        self.assertContains(response, "Progress: 3h 25min")
        self.assertContains(response, "April 13, 2026 - May 12, 2026")
        self.assertContains(response, 'data-track-action-root', html=False)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["closeModal"]["formId"], "track-form-test")
        self.assertEqual(trigger["showToast"]["type"], "success")
        self.assertIn("Added", trigger["showToast"]["message"])
        self.assertTrue(
            Game.objects.filter(
                item__media_id="game-123",
                user=self.user,
                progress=205,
                status=Status.PLANNING.value,
            ).exists(),
        )

    @patch("app.save_views.ensure_item_metadata")
    @override_settings(TRACK_TIME=True)
    def test_create_game_backfills_start_date_from_progress(
        self,
        ensure_item_metadata_mock,
    ):
        """New game saves should infer start_date from progress when it is missing."""
        item = Item.objects.create(
            media_id="wordle-1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Wordle",
            image="http://example.com/wordle.jpg",
        )
        ensure_item_metadata_mock.return_value = SimpleNamespace(item=item)

        response = self.client.post(
            reverse("media_save"),
            {
                "media_id": "wordle-1",
                "source": Sources.IGDB.value,
                "media_type": MediaTypes.GAME.value,
                "status": Status.PLANNING.value,
                "progress": "5min",
                "end_date": "2026-05-12T12:00",
            },
        )

        self.assertEqual(response.status_code, 302)
        game = Game.objects.get(item__media_id="wordle-1")
        self.assertEqual(game.progress, 5)
        self.assertIsNotNone(game.start_date)
        self.assertEqual(game.start_date, game.end_date - datetime.timedelta(minutes=5))

    @patch("app.save_views.ensure_item_metadata")
    @override_settings(TRACK_TIME=True)
    def test_create_game_respects_cleared_flag(self, ensure_item_metadata_mock):
        """start_date_cleared=1 on a create form must prevent server from auto-calculating."""
        item = Item.objects.create(
            media_id="wordle-4",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Wordle",
            image="http://example.com/wordle.jpg",
        )
        ensure_item_metadata_mock.return_value = SimpleNamespace(item=item)

        response = self.client.post(
            reverse("media_save"),
            {
                "media_id": "wordle-4",
                "source": Sources.IGDB.value,
                "media_type": MediaTypes.GAME.value,
                "status": Status.PLANNING.value,
                "progress": "20min",
                "end_date": "2026-05-12T12:00",
                "start_date": "",
                "start_date_cleared": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        game = Game.objects.get(item__media_id="wordle-4")
        self.assertIsNone(game.start_date)

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_grouped_anime_episode_uses_provider_source(
        self,
        view_metadata_mock,
        model_metadata_mock,
    ):
        """Grouped-anime episode saves should keep provider source and anime library type."""
        tv_metadata = {
            "media_id": "76703",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Pokemon",
            "original_title": "Pokemon",
            "localized_title": "Pokemon",
            "image": "http://example.com/pokemon.jpg",
            "details": {"seasons": 2},
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "image": "http://example.com/indigo.jpg",
                    },
                    {
                        "season_number": 2,
                        "image": "http://example.com/orange.jpg",
                    },
                ],
            },
        }
        tv_with_seasons_metadata = {
            **tv_metadata,
            "season/2": {
                "media_id": "76703",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 2,
                "season_title": "Orange Islands",
                "image": "http://example.com/orange.jpg",
                "details": {"episodes": 2},
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "1999-01-28",
                        "image": "http://example.com/orange-1.jpg",
                        "name": "Pallet Party Panic",
                    },
                    {
                        "episode_number": 2,
                        "air_date": "1999-02-04",
                        "image": "http://example.com/orange-2.jpg",
                        "name": "A Scare in the Air",
                    },
                ],
            },
        }

        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            **_kwargs,
        ):
            self.assertEqual(media_id, "76703")
            self.assertEqual(source, Sources.TVDB.value)
            if media_type == "tv_with_seasons":
                self.assertEqual(season_numbers, [2])
                return tv_with_seasons_metadata
            if media_type == MediaTypes.SEASON.value:
                self.assertEqual(season_numbers, [2])
                return tv_with_seasons_metadata["season/2"]
            if media_type == MediaTypes.TV.value:
                return tv_metadata
            error_message = f"Unexpected metadata request: {media_type}"
            raise AssertionError(error_message)

        view_metadata_mock.side_effect = metadata_side_effect
        model_metadata_mock.side_effect = metadata_side_effect

        response = self.client.post(
            reverse("episode_save"),
            {
                "media_id": "76703",
                "season_number": 2,
                "episode_number": 1,
                "source": Sources.TVDB.value,
                "library_media_type": MediaTypes.ANIME.value,
                "end_date": "2024-01-01",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_episode = Episode.objects.get(
            item__media_id="76703",
            item__source=Sources.TVDB.value,
            item__episode_number=1,
            related_season__user=self.user,
        )
        self.assertEqual(created_episode.related_season.item.source, Sources.TVDB.value)
        self.assertEqual(
            created_episode.related_season.item.library_media_type,
            MediaTypes.ANIME.value,
        )
        self.assertEqual(
            created_episode.related_season.related_tv.item.library_media_type,
            MediaTypes.ANIME.value,
        )

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_grouped_anime_episode_prefers_route_source_over_tmdb_post_value(
        self,
        view_metadata_mock,
        model_metadata_mock,
    ):
        """Grouped-anime episode saves should follow the provider encoded in the details route."""
        tv_metadata = {
            "media_id": "267440",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Attack on Titan",
            "original_title": "進撃の巨人",
            "localized_title": "Attack on Titan",
            "image": "http://example.com/aot.jpg",
            "details": {"seasons": 4},
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "image": "http://example.com/aot-s1.jpg",
                    },
                ],
            },
        }
        tv_with_seasons_metadata = {
            **tv_metadata,
            "season/1": {
                "media_id": "267440",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "season_title": "Season 1",
                "image": "http://example.com/aot-s1.jpg",
                "details": {"episodes": 1},
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "2013-04-07",
                        "image": "http://example.com/aot-e1.jpg",
                        "name": "To You, in 2000 Years",
                    },
                ],
            },
        }

        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            **_kwargs,
        ):
            self.assertEqual(media_id, "267440")
            self.assertEqual(source, Sources.TVDB.value)
            if media_type == "tv_with_seasons":
                self.assertEqual(season_numbers, [1])
                return tv_with_seasons_metadata
            if media_type == MediaTypes.TV.value:
                return tv_metadata
            error_message = f"Unexpected metadata request: {media_type}"
            raise AssertionError(error_message)

        view_metadata_mock.side_effect = metadata_side_effect
        model_metadata_mock.side_effect = metadata_side_effect

        response = self.client.post(
            f"{reverse('episode_save')}?next=/details/tvdb/tv/267440/attack-on-titan/season/1",
            {
                "media_id": "267440",
                "season_number": 1,
                "episode_number": 1,
                "source": Sources.TMDB.value,
                "library_media_type": MediaTypes.ANIME.value,
                "end_date": "2024-01-01",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_episode = Episode.objects.get(
            item__media_id="267440",
            item__source=Sources.TVDB.value,
            item__episode_number=1,
            related_season__user=self.user,
        )
        self.assertEqual(created_episode.related_season.item.source, Sources.TVDB.value)


class EditMedia(TestCase):
    """Test the editing of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_edit_movie_score(self):
        """Test the editing of a movie score."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            score=9,
            progress=1,
            status=Status.COMPLETED.value,
            notes="Nice",
            start_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

        response = self.client.post(
            reverse("media_save"),
            {
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "score": 10,
                "progress": 1,
                "status": Status.COMPLETED.value,
                "notes": "Nice",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Movie.objects.get(item__media_id="10494").score, 10)

    def test_edit_movie_htmx_returns_inline_detail_update(self):
        """HTMX saves should update the detail tracker in place."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            score=9,
            progress=1,
            status=Status.IN_PROGRESS.value,
            notes="Nice",
            start_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )
        initial_status = Movie.objects.get(item__media_id="10494").status

        response = self.client.post(
            f"{reverse('media_save')}?next=/details/tmdb/movie/10494/perfect-blue",
            {
                "track_form_id": "track-form-test",
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "score": 10,
                "progress": 1,
                "status": Status.COMPLETED.value,
                "notes": "Nice",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-track-action-root', html=False)
        self.assertContains(response, 'id="track-action-movie-10494"', html=False)
        self.assertContains(response, f'id="detail-score-chip-{movie.id}"', html=False)
        self.assertContains(response, "Edit rating")
        self.assertContains(response, "Completed")
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["closeModal"]["formId"], "track-form-test")
        self.assertEqual(trigger["showToast"]["type"], "success")
        self.assertIn("Updated", trigger["showToast"]["message"])
        self.assertTrue(
            Movie.objects.filter(
                item__media_id="10494",
                score=10,
                status=Status.COMPLETED.value,
            ).exists(),
        )

    @override_settings(TRACK_TIME=True)
    def test_edit_game_preserves_existing_start_date(self):
        """Editing an existing game should not rewrite an already-set start date."""
        item = Item.objects.create(
            media_id="wordle-1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Wordle",
            image="http://example.com/wordle.jpg",
        )
        start_date = datetime.datetime(2026, 5, 12, 11, 40, tzinfo=datetime.UTC)
        end_date = datetime.datetime(2026, 5, 12, 12, 0, tzinfo=datetime.UTC)
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=20,
            start_date=start_date,
            end_date=end_date,
        )

        response = self.client.post(
            reverse("media_save"),
            {
                "instance_id": game.id,
                "media_id": "wordle-1",
                "source": Sources.IGDB.value,
                "media_type": MediaTypes.GAME.value,
                "status": Status.PLANNING.value,
                "progress": "10min",
                "start_date": "2026-05-12T11:40",
                "end_date": "2026-05-12T12:00",
            },
        )

        self.assertEqual(response.status_code, 302)
        game.refresh_from_db()
        self.assertEqual(game.start_date, start_date)
        self.assertEqual(game.end_date, end_date)
        self.assertEqual(game.progress, 10)

    def test_edit_game_clears_start_date_when_start_date_cleared_flag_set(self):
        """start_date_cleared=1 must prevent the server from recalculating start_date."""
        item = Item.objects.create(
            media_id="wordle-2",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Wordle",
            image="http://example.com/wordle.jpg",
        )
        start_date = datetime.datetime(2026, 5, 12, 11, 40, tzinfo=datetime.UTC)
        end_date = datetime.datetime(2026, 5, 12, 12, 0, tzinfo=datetime.UTC)
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=20,
            start_date=start_date,
            end_date=end_date,
        )

        response = self.client.post(
            reverse("media_save"),
            {
                "instance_id": game.id,
                "media_id": "wordle-2",
                "source": Sources.IGDB.value,
                "media_type": MediaTypes.GAME.value,
                "status": Status.PLANNING.value,
                "progress": "20min",
                "end_date": "2026-05-12T12:00",
                "start_date": "",
                "start_date_cleared": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        game.refresh_from_db()
        self.assertIsNone(game.start_date)

    def test_edit_game_clears_start_date_when_db_had_start_date(self):
        """Clearing start_date without the sentinel flag still works when the DB had a value."""
        item = Item.objects.create(
            media_id="wordle-3",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Wordle",
            image="http://example.com/wordle.jpg",
        )
        start_date = datetime.datetime(2026, 5, 12, 11, 40, tzinfo=datetime.UTC)
        end_date = datetime.datetime(2026, 5, 12, 12, 0, tzinfo=datetime.UTC)
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=20,
            start_date=start_date,
            end_date=end_date,
        )

        response = self.client.post(
            reverse("media_save"),
            {
                "instance_id": game.id,
                "media_id": "wordle-3",
                "source": Sources.IGDB.value,
                "media_type": MediaTypes.GAME.value,
                "status": Status.PLANNING.value,
                "progress": "20min",
                "end_date": "2026-05-12T12:00",
                "start_date": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        game.refresh_from_db()
        self.assertIsNone(game.start_date)

    @patch("app.views.ensure_item_metadata")
    def test_create_movie_htmx_inserts_score_chip_slot(self, ensure_item_metadata_mock):
        """HTMX tracker creation should insert the score chip into the page."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/image.jpg",
        )
        ensure_item_metadata_mock.return_value = SimpleNamespace(item=item)

        response = self.client.post(
            f"{reverse('media_save')}?next=/details/tmdb/movie/10494/perfect-blue",
            {
                "track_form_id": "track-form-test",
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "score": 8.7,
                "progress": 1,
                "status": Status.COMPLETED.value,
                "notes": "Nice",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'id="detail-score-chip-slot-movie-10494-tmdb"',
            html=False,
        )
        self.assertContains(response, f'id="detail-score-chip-{Movie.objects.get(item__media_id="10494").id}"', html=False)
        self.assertContains(response, "Edit rating")
        self.assertContains(response, "Completed")
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["closeModal"]["formId"], "track-form-test")
        self.assertEqual(trigger["showToast"]["type"], "success")
        self.assertIn("Added", trigger["showToast"]["message"])
        self.assertTrue(
            Movie.objects.filter(
                item__media_id="10494",
                score=8.7,
                status=Status.COMPLETED.value,
            ).exists(),
        )

    def test_edit_movie_htmx_validation_error_reopens_modal(self):
        """Invalid HTMX saves should re-render the modal and reopen it."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            score=9,
            progress=1,
            status=Status.IN_PROGRESS.value,
            notes="Nice",
            start_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )
        initial_status = Movie.objects.get(item__media_id="10494").status

        response = self.client.post(
            f"{reverse('media_save')}?next=/details/tmdb/movie/10494/perfect-blue",
            {
                "track_form_id": "track-form-test",
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "score": 10,
                "progress": 1,
                "status": "bogus",
                "notes": "Nice",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-track-action-root', html=False)
        self.assertContains(response, "trackOpen: true", html=False)
        self.assertContains(response, "track-form-test")
        self.assertContains(response, "data-track-modal-root", html=False)
        self.assertContains(
            response,
            'hx-target="closest [data-track-action-root]"',
            html=False,
        )
        self.assertContains(response, 'hx-swap="outerHTML"', html=False)
        self.assertEqual(
            Movie.objects.get(item__media_id="10494").status,
            initial_status,
        )

    def test_edit_movie_image_url(self):
        """Test overriding a movie image URL from the edit form."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/original.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.client.post(
            reverse("media_save"),
            {
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "status": Status.PLANNING.value,
                "image_url": "https://images.example.com/custom-poster.jpg",
            },
        )

        item.refresh_from_db()
        self.assertEqual(item.image, "https://images.example.com/custom-poster.jpg")


class DeleteMedia(TestCase):
    """Test the deletion of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item_season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.season = Season.objects.create(
            item=self.item_season,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.item_ep = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
            episode_number=99,
        )
        self.episode = Episode.objects.create(
            item=self.item_ep,
            related_season=self.season,
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

    def test_delete_tv(self):
        """Test the deletion of a tv through views."""
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        tv_obj = TV.objects.get(user=self.user)

        self.client.post(
            reverse("media_delete"),
            data={
                "instance_id": tv_obj.id,
                "media_type": MediaTypes.TV.value,
            },
        )

        self.assertEqual(Movie.objects.filter(user=self.user).count(), 0)

    def test_delete_season(self):
        """Test the deletion of a season through views."""
        self.client.post(
            reverse(
                "media_delete",
            ),
            data={"instance_id": self.season.id, "media_type": MediaTypes.SEASON.value},
        )

        self.assertEqual(Season.objects.filter(user=self.user).count(), 0)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )

    def test_unwatch_episode(self):
        """Test unwatching of an episode through views."""
        self.client.post(
            reverse("media_delete"),
            data={
                "instance_id": self.episode.id,
                "media_type": MediaTypes.EPISODE.value,
            },
        )

        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )
