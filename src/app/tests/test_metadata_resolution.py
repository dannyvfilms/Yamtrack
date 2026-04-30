from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.utils import OperationalError
from django.test import TestCase, override_settings

from app.models import (
    Item,
    ItemProviderLink,
    MediaTypes,
    MetadataProviderPreference,
    Sources,
)
from app.services import metadata_resolution


class MetadataResolutionTests(TestCase):
    """Tests for per-user metadata provider resolution."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="resolver",
            password="pw12345",
        )

    def test_metadata_default_source_falls_back_when_tvdb_is_disabled(self):
        """TV defaults should fall back to TMDB when TVDB is unavailable."""
        self.user.tv_metadata_source_default = Sources.TVDB.value

        with override_settings(TVDB_API_KEY=""):
            provider = metadata_resolution.metadata_default_source(
                self.user,
                MediaTypes.TV.value,
            )

        self.assertEqual(provider, Sources.TMDB.value)

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.services.metadata_resolution.services.get_media_metadata")
    def test_resolve_detail_metadata_uses_provider_override_without_changing_tracking(
        self,
        mock_get_media_metadata,
    ):
        """Display-provider overrides should not mutate the tracked provider."""
        item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/breaking-bad.jpg",
        )
        MetadataProviderPreference.objects.create(
            user=self.user,
            item=item,
            provider=Sources.TVDB.value,
        )
        ItemProviderLink.objects.create(
            item=item,
            provider=Sources.TVDB.value,
            provider_media_id="81189",
            provider_media_type=MediaTypes.TV.value,
        )
        base_metadata = {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Breaking Bad",
            "image": "https://example.com/breaking-bad.jpg",
            "source_url": "https://www.themoviedb.org/tv/1396",
            "external_links": {"TMDB": "https://www.themoviedb.org/tv/1396"},
            "details": {"episodes": 62, "status": "Tracked"},
            "related": {"recommendations": [{"media_id": "1"}]},
        }
        mock_get_media_metadata.return_value = {
            "media_id": "81189",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Breaking Bad (TVDB)",
            "image": "https://example.com/breaking-bad-tvdb.jpg",
            "source_url": "https://www.thetvdb.com/dereferrer/series/81189",
            "external_links": {"TVDB": "https://www.thetvdb.com/dereferrer/series/81189"},
            "details": {"episodes": 999, "status": "Overlay"},
            "related": {"seasons": [{"season_number": 1}]},
        }

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=item,
            route_media_type=MediaTypes.TV.value,
            media_id=item.media_id,
            source=item.source,
            base_metadata=base_metadata,
        )

        self.assertEqual(result.display_provider, Sources.TVDB.value)
        self.assertEqual(result.identity_provider, Sources.TMDB.value)
        self.assertEqual(result.mapping_status, "mapped")
        self.assertEqual(result.provider_media_id, "81189")
        self.assertEqual(result.header_metadata["title"], "Breaking Bad (TVDB)")
        self.assertEqual(
            result.header_metadata["source_url"],
            "https://www.themoviedb.org/tv/1396",
        )
        self.assertEqual(
            result.header_metadata["display_source_url"],
            "https://www.thetvdb.com/dereferrer/series/81189",
        )
        self.assertEqual(result.header_metadata["details"]["status"], "Tracked")
        self.assertEqual(
            result.header_metadata["related"],
            {"recommendations": [{"media_id": "1"}]},
        )
        self.assertEqual(
            result.header_metadata["external_links"],
            {
                "TMDB": "https://www.themoviedb.org/tv/1396",
                "TVDB": "https://www.thetvdb.com/dereferrer/series/81189",
            },
        )
        self.assertEqual(item.source, Sources.TMDB.value)

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_resolve_detail_metadata_defaults_to_tracking_source_without_preference(self):
        """Tracked titles should keep showing metadata from their own source by default."""
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        self.user.anime_metadata_source_default = Sources.TVDB.value

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=item,
            route_media_type=MediaTypes.ANIME.value,
            media_id=item.media_id,
            source=item.source,
            base_metadata={
                "media_id": "52991",
                "source": Sources.MAL.value,
                "media_type": MediaTypes.ANIME.value,
                "title": "Frieren",
                "image": "https://example.com/frieren.jpg",
                "details": {"episodes": 28},
                "related": {},
            },
        )

        self.assertEqual(result.display_provider, Sources.MAL.value)
        self.assertEqual(result.identity_provider, Sources.MAL.value)
        self.assertEqual(result.mapping_status, "identity")

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_resolve_detail_metadata_uses_requested_source_for_untracked_result(self):
        """Explicit search-result routes should show their own provider metadata by default."""
        self.user.anime_metadata_source_default = Sources.TVDB.value

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=None,
            route_media_type=MediaTypes.ANIME.value,
            media_id="52991",
            source=Sources.MAL.value,
            base_metadata={
                "media_id": "52991",
                "source": Sources.MAL.value,
                "media_type": MediaTypes.ANIME.value,
                "title": "Frieren",
                "image": "https://example.com/frieren.jpg",
                "details": {"episodes": 28},
                "related": {},
            },
        )

        self.assertEqual(result.display_provider, Sources.MAL.value)
        self.assertEqual(result.identity_provider, Sources.MAL.value)
        self.assertEqual(result.mapping_status, "identity")

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_resolve_detail_metadata_marks_missing_mapping(self):
        """Missing cross-provider mappings should be surfaced without switching tracking."""
        item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/breaking-bad.jpg",
        )
        MetadataProviderPreference.objects.create(
            user=self.user,
            item=item,
            provider=Sources.TVDB.value,
        )
        base_metadata = {
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Breaking Bad",
            "image": "https://example.com/breaking-bad.jpg",
            "details": {"episodes": 62},
            "related": {},
        }

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=item,
            route_media_type=MediaTypes.TV.value,
            media_id=item.media_id,
            source=item.source,
            base_metadata=base_metadata,
        )

        self.assertEqual(result.display_provider, Sources.TVDB.value)
        self.assertEqual(result.mapping_status, "missing")
        self.assertIsNone(result.provider_media_id)
        self.assertEqual(result.header_metadata["title"], "Breaking Bad")

    def test_resolve_detail_metadata_uses_custom_overlay_for_movie(self):
        """Custom provider preferences should overlay stored metadata on normal items."""
        item = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
            original_title="The Matrix",
            localized_title="The Matrix",
            image="https://example.com/custom-matrix.jpg",
            genres=["Action", "Sci-Fi"],
            runtime="2h 16min",
            manual_metadata={
                "title": "The Matrix (Custom)",
                "original_title": "Matrix Original",
                "localized_title": "Matrix Localized",
                "image": "https://example.com/custom-matrix.jpg",
                "synopsis": "Custom synopsis.",
                "genres": ["Action", "Sci-Fi"],
                "details": {
                    "release_date": "1999-03-31",
                    "runtime": "2h 16min",
                    "status": "Released",
                },
            },
        )
        MetadataProviderPreference.objects.create(
            user=self.user,
            item=item,
            provider=Sources.MANUAL.value,
        )
        base_metadata = {
            "media_id": "603",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "title": "The Matrix",
            "image": "https://example.com/provider-matrix.jpg",
            "synopsis": "Provider synopsis.",
            "genres": ["Action"],
            "details": {
                "release_date": "1999-01-01",
                "runtime": "2h 10min",
                "status": "Provider",
            },
            "related": {"recommendations": [{"media_id": "604"}]},
        }

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=item,
            route_media_type=MediaTypes.MOVIE.value,
            media_id=item.media_id,
            source=item.source,
            base_metadata=base_metadata,
        )

        self.assertEqual(result.display_provider, Sources.MANUAL.value)
        self.assertEqual(result.identity_provider, Sources.TMDB.value)
        self.assertEqual(result.mapping_status, "custom")
        self.assertEqual(result.provider_media_id, f"item:{item.id}")
        self.assertEqual(result.header_metadata["title"], "The Matrix (Custom)")
        self.assertEqual(
            result.header_metadata["synopsis"],
            "Custom synopsis.",
        )
        self.assertEqual(
            result.header_metadata["details"]["release_date"],
            "1999-03-31",
        )
        self.assertEqual(
            result.header_metadata["details"]["status"],
            "Released",
        )
        self.assertEqual(
            result.header_metadata["related"],
            {"recommendations": [{"media_id": "604"}]},
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.services.metadata_resolution.anime_mapping.resolve_provider_series_id")
    def test_resolve_provider_media_id_maps_flat_anime_via_mapping_fallback(
        self,
        mock_resolve_provider_series_id,
    ):
        """Flat MAL anime should persist a provider link when grouped mapping exists."""
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        mock_resolve_provider_series_id.return_value = "9350138"

        provider_media_id = metadata_resolution.resolve_provider_media_id(
            item,
            Sources.TVDB.value,
            route_media_type=MediaTypes.ANIME.value,
        )

        self.assertEqual(provider_media_id, "9350138")
        self.assertTrue(
            ItemProviderLink.objects.filter(
                item=item,
                provider=Sources.TVDB.value,
                provider_media_id="9350138",
                provider_media_type=MediaTypes.TV.value,
            ).exists(),
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.services.metadata_resolution.services.get_media_metadata")
    def test_resolve_detail_metadata_adds_grouped_preview_target_for_flat_mal_anime(
        self,
        mock_get_media_metadata,
    ):
        """Flat MAL anime preview should expose the grouped season and episode range."""
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        MetadataProviderPreference.objects.create(
            user=self.user,
            item=item,
            provider=Sources.TVDB.value,
        )
        base_metadata = {
            "media_id": "52991",
            "source": Sources.MAL.value,
            "media_type": MediaTypes.ANIME.value,
            "title": "Frieren",
            "image": "https://example.com/frieren.jpg",
            "details": {"episodes": 28},
            "related": {},
        }
        mock_get_media_metadata.side_effect = [
            {
                "media_id": "9350138",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.ANIME.value,
                "title": "Frieren: Beyond Journey's End",
                "related": {"seasons": [{"season_number": 1}]},
                "external_links": {},
            },
            {
                "media_id": "9350138",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.ANIME.value,
                "title": "Frieren: Beyond Journey's End",
                "related": {
                    "seasons": [
                        {
                            "season_number": 1,
                            "episode_count": 28,
                        },
                    ],
                },
                "season/1": {
                    "season_number": 1,
                    "season_title": "Season 1",
                    "details": {"episodes": 28},
                },
            },
        ]

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=item,
            route_media_type=MediaTypes.ANIME.value,
            media_id=item.media_id,
            source=item.source,
            base_metadata=base_metadata,
        )

        self.assertEqual(result.mapping_status, "mapped")
        self.assertEqual(
            result.grouped_preview_target,
            {
                "season_number": 1,
                "episode_offset": 0,
                "episode_total": 28,
                "episode_start": 1,
                "episode_end": 28,
                "season_title": "Season 1",
                "season_episode_count": 28,
                "first_air_date": None,
            },
        )
        self.assertTrue(result.grouped_preview["related"]["seasons"][0]["is_mapped_target"])
        self.assertEqual(
            result.grouped_preview["related"]["seasons"][0]["mapped_episode_start"],
            1,
        )
        self.assertEqual(
            result.grouped_preview["related"]["seasons"][0]["mapped_episode_end"],
            28,
        )

    @patch("app.db_retry.time.sleep")
    @patch("app.services.metadata_resolution.ItemProviderLink.objects.update_or_create")
    def test_resolve_detail_metadata_best_effort_keeps_identity_payload_on_lock(
        self,
        mock_update_or_create,
        _mock_sleep,
    ):
        """Identity-provider detail reads should render even if link persistence locks."""
        item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/breaking-bad.jpg",
        )
        mock_update_or_create.side_effect = OperationalError("database is locked")

        result = metadata_resolution.resolve_detail_metadata(
            self.user,
            item=item,
            route_media_type=MediaTypes.TV.value,
            media_id=item.media_id,
            source=item.source,
            base_metadata={
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "title": "Breaking Bad",
                "image": "https://example.com/breaking-bad.jpg",
                "details": {"episodes": 62},
                "related": {},
            },
            persistence_mode="best_effort",
        )

        self.assertEqual(result.display_provider, Sources.TMDB.value)
        self.assertEqual(result.mapping_status, "identity")
        self.assertEqual(result.header_metadata["title"], "Breaking Bad")
        self.assertEqual(mock_update_or_create.call_count, 12)

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.db_retry.time.sleep")
    @patch("app.services.metadata_resolution.anime_mapping.resolve_provider_series_id")
    @patch("app.services.metadata_resolution.ItemProviderLink.objects.update_or_create")
    def test_resolve_provider_media_id_best_effort_returns_mapping_on_lock(
        self,
        mock_update_or_create,
        mock_resolve_provider_series_id,
        _mock_sleep,
    ):
        """Grouped-anime mapping should still resolve even when link writes defer."""
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        mock_resolve_provider_series_id.return_value = "9350138"
        mock_update_or_create.side_effect = OperationalError("database is locked")

        provider_media_id = metadata_resolution.resolve_provider_media_id(
            item,
            Sources.TVDB.value,
            route_media_type=MediaTypes.ANIME.value,
            persistence_mode="best_effort",
        )

        self.assertEqual(provider_media_id, "9350138")
        self.assertFalse(
            ItemProviderLink.objects.filter(
                item=item,
                provider=Sources.TVDB.value,
                provider_media_id="9350138",
                provider_media_type=MediaTypes.TV.value,
            ).exists(),
        )
        self.assertEqual(mock_update_or_create.call_count, 6)

    @patch("app.db_retry.time.sleep")
    @patch("app.services.metadata_resolution.ItemProviderLink.objects.update_or_create")
    def test_upsert_provider_links_required_mode_raises_on_lock(
        self,
        mock_update_or_create,
        _mock_sleep,
    ):
        """Required persistence should still raise after bounded retries."""
        item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/breaking-bad.jpg",
        )
        mock_update_or_create.side_effect = OperationalError("database is locked")

        with self.assertRaises(OperationalError):
            metadata_resolution.upsert_provider_links(
                item,
                {
                    "media_id": "1396",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "title": "Breaking Bad",
                    "image": "https://example.com/breaking-bad.jpg",
                },
                provider=Sources.TMDB.value,
                provider_media_type=MediaTypes.TV.value,
            )

        self.assertEqual(mock_update_or_create.call_count, 6)
