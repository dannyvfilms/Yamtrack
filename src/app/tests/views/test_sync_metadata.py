from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Item, MediaTypes, Sources

User = get_user_model()


class SyncMetadataViewTests(TestCase):
    def setUp(self):
        self.credentials = {"username": "sync-user", "password": "12345"}
        self.user = User.objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.views._sync_plex_rating")
    @patch("app.views.Item.fetch_releases")
    @patch("app.views.game_length_services.refresh_game_lengths")
    @patch("app.views.services.get_media_metadata")
    def test_sync_metadata_refreshes_game_lengths_for_igdb_games(
        self,
        mock_get_media_metadata,
        mock_refresh_game_lengths,
        mock_fetch_releases,
        mock_sync_plex_rating,
    ):
        mock_get_media_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "https://example.com/dispatch.jpg",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.post(
            reverse(
                "sync_metadata",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                },
            ),
            {"next": "/"},
        )

        self.assertEqual(response.status_code, 302)
        mock_refresh_game_lengths.assert_called_once()
        _, kwargs = mock_refresh_game_lengths.call_args
        self.assertTrue(kwargs["force"])
        self.assertTrue(kwargs["fetch_hltb"])
        mock_fetch_releases.assert_called_once()
        mock_sync_plex_rating.assert_called_once()

    @patch("app.views._sync_plex_rating")
    @patch("app.views.Item.fetch_releases")
    @patch("app.views.trakt_popularity_service.refresh_trakt_popularity")
    @patch("app.views.services.get_media_metadata")
    def test_sync_metadata_refreshes_trakt_popularity_for_movies(
        self,
        mock_get_media_metadata,
        mock_refresh_trakt_popularity,
        mock_fetch_releases,
        mock_sync_plex_rating,
    ):
        mock_get_media_metadata.return_value = {
            "media_id": "238",
            "title": "The Godfather",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "https://example.com/godfather.jpg",
            "details": {
                "release_date": "1972-03-14",
            },
            "related": {},
        }

        response = self.client.post(
            reverse(
                "sync_metadata",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
            {"next": "/"},
        )

        self.assertEqual(response.status_code, 302)
        mock_refresh_trakt_popularity.assert_called_once()
        _, kwargs = mock_refresh_trakt_popularity.call_args
        self.assertEqual(kwargs["route_media_type"], MediaTypes.MOVIE.value)
        self.assertTrue(kwargs["force"])
        mock_fetch_releases.assert_called_once()
        mock_sync_plex_rating.assert_called_once()

    @patch("app.views._sync_plex_rating")
    @patch("app.views.Item.fetch_releases")
    @patch("app.views.credits.sync_item_credits_from_metadata")
    @patch("app.views.trakt_popularity_service.refresh_trakt_popularity")
    @patch("app.views.metadata_resolution.upsert_provider_links")
    @patch("app.views.services.get_media_metadata")
    def test_sync_metadata_preserves_tmdb_tv_anime_genre_supplement(
        self,
        mock_get_media_metadata,
        mock_upsert_provider_links,
        mock_refresh_trakt_popularity,
        mock_sync_item_credits,
        mock_fetch_releases,
        mock_sync_plex_rating,
    ):
        item = Item.objects.create(
            media_id="2002",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Manual Refresh Anime",
            image="https://example.com/manual-refresh-anime.jpg",
            genres=["Comedy", "Anime"],
        )
        mock_get_media_metadata.return_value = {
            "media_id": item.media_id,
            "title": item.title,
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": item.image,
            "genres": ["Comedy"],
            "details": {
                "format": "TV",
                "release_date": "2024-01-01",
            },
            "related": {},
        }

        response = self.client.post(
            reverse(
                "sync_metadata",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": item.media_id,
                },
            ),
            {"next": "/"},
        )

        item.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(item.genres, ["Comedy", "Anime"])
        mock_upsert_provider_links.assert_called_once()
        mock_refresh_trakt_popularity.assert_called_once()
        mock_sync_item_credits.assert_called_once()
        mock_fetch_releases.assert_called_once()
        mock_sync_plex_rating.assert_called_once()
