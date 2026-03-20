from unittest.mock import patch

from django.contrib.auth import get_user_model
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
