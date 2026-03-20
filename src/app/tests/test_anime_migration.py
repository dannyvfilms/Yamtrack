from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import (
    Anime,
    Episode,
    Item,
    ItemProviderLink,
    MediaTypes,
    MetadataProviderPreference,
    Season,
    Sources,
    Status,
    TV,
)
from app.services import anime_migration
from app.services.tracking_hydration import HydratedItemResult


class AnimeMigrationTests(TestCase):
    """Tests for explicit flat-anime to grouped-series migration."""

    def setUp(self):
        self.metadata_patcher = patch(
            "app.models.providers.services.get_media_metadata",
            return_value={
                "max_progress": 12,
                "details": {"episodes": 12},
            },
        )
        self.metadata_patcher.start()
        self.addCleanup(self.metadata_patcher.stop)
        self.user = get_user_model().objects.create_user(
            username="anime-migrate",
            password="pw12345",
        )
        self.flat_item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        self.flat_entry = Anime.objects.create(
            item=self.flat_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
            score=9,
            notes="Great adaptation",
            start_date=timezone.now() - timedelta(days=3),
            end_date=timezone.now(),
        )
        self.grouped_item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Frieren: Beyond Journey's End",
            image="https://example.com/grouped.jpg",
        )

    @patch("app.services.anime_migration.anime_mapping.find_entries_for_mal_id")
    @patch("app.services.anime_migration.anime_mapping.resolve_provider_series_id")
    @patch("app.services.anime_migration.services.get_media_metadata")
    @patch("app.services.anime_migration.ensure_item_metadata")
    def test_migrate_flat_anime_to_grouped_series(
        self,
        mock_ensure_item_metadata,
        mock_get_media_metadata,
        mock_resolve_provider_series_id,
        mock_find_entries_for_mal_id,
    ):
        """Migration should create grouped TV/season/episode records and archive flat rows."""
        mock_resolve_provider_series_id.return_value = "9350138"
        mock_find_entries_for_mal_id.return_value = [
            {
                "mal_id": "52991",
                "tvdb_id": "9350138",
                "tvdb_season": 1,
                "tvdb_epoffset": 0,
            },
        ]
        mock_ensure_item_metadata.return_value = HydratedItemResult(
            item=self.grouped_item,
            metadata={
                "media_id": "9350138",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.ANIME.value,
                "identity_media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.ANIME.value,
                "title": "Frieren: Beyond Journey's End",
                "image": "https://example.com/grouped.jpg",
                "related": {"seasons": [{"season_number": 1}]},
            },
            created=False,
        )
        mock_get_media_metadata.return_value = {
            "related": {"seasons": [{"season_number": 1}]},
            "season/1": {
                "title": "Frieren: Beyond Journey's End",
                "image": "https://example.com/season1.jpg",
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": "2023-09-29", "runtime": 24},
                    {"episode_number": 2, "air_date": "2023-10-06", "runtime": 24},
                    {"episode_number": 3, "air_date": "2023-10-13", "runtime": 24},
                ],
            },
        }

        result = anime_migration.migrate_flat_anime_to_grouped(
            self.user,
            self.flat_item,
            Sources.TVDB.value,
        )

        grouped_tv = TV.objects.get(item=self.grouped_item, user=self.user)
        grouped_season = Season.objects.get(
            item__media_id="9350138",
            item__source=Sources.TVDB.value,
            item__season_number=1,
            user=self.user,
        )
        watched_episodes = list(
            Episode.objects.filter(related_season=grouped_season).order_by("item__episode_number")
        )

        self.assertEqual(result.grouped_tv, grouped_tv)
        self.assertEqual(len(watched_episodes), 3)
        self.assertEqual(grouped_tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(grouped_tv.score, 9)
        self.assertEqual(grouped_tv.notes, "Great adaptation")
        self.assertEqual(watched_episodes[0].item.library_media_type, MediaTypes.ANIME.value)
        self.assertEqual(watched_episodes[0].end_date, self.flat_entry.start_date)
        self.assertLessEqual(
            abs(watched_episodes[-1].end_date - self.flat_entry.end_date),
            timedelta(milliseconds=1),
        )
        self.assertFalse(
            Anime.objects.filter(user=self.user, item=self.flat_item).exists(),
        )
        self.flat_entry.refresh_from_db()
        self.assertEqual(self.flat_entry.migrated_to_item, self.grouped_item)
        self.assertTrue(
            MetadataProviderPreference.objects.filter(
                user=self.user,
                item=self.grouped_item,
                provider=Sources.TVDB.value,
            ).exists(),
        )
        self.assertTrue(
            ItemProviderLink.objects.filter(
                item=self.flat_item,
                provider=Sources.TVDB.value,
                provider_media_id="9350138",
                provider_media_type=MediaTypes.TV.value,
                season_number=1,
                episode_offset=0,
            ).exists(),
        )

    @patch("app.services.anime_migration.anime_mapping.find_entries_for_mal_id")
    @patch("app.services.anime_migration.anime_mapping.resolve_provider_series_id")
    @patch("app.services.anime_migration.services.get_media_metadata")
    @patch("app.services.anime_migration.ensure_item_metadata")
    def test_migration_blocks_when_progress_exceeds_mapped_season(
        self,
        mock_ensure_item_metadata,
        mock_get_media_metadata,
        mock_resolve_provider_series_id,
        mock_find_entries_for_mal_id,
    ):
        """Migration should stop instead of truncating episode progress."""
        Anime.all_objects.filter(id=self.flat_entry.id).update(progress=4)
        self.flat_entry.refresh_from_db()
        mock_resolve_provider_series_id.return_value = "9350138"
        mock_find_entries_for_mal_id.return_value = [
            {
                "mal_id": "52991",
                "tvdb_id": "9350138",
                "tvdb_season": 1,
                "tvdb_epoffset": 0,
            },
        ]
        mock_ensure_item_metadata.return_value = HydratedItemResult(
            item=self.grouped_item,
            metadata={
                "media_id": "9350138",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.ANIME.value,
                "identity_media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.ANIME.value,
                "title": "Frieren: Beyond Journey's End",
                "image": "https://example.com/grouped.jpg",
                "related": {"seasons": [{"season_number": 1}]},
            },
            created=False,
        )
        mock_get_media_metadata.return_value = {
            "related": {"seasons": [{"season_number": 1}]},
            "season/1": {
                "title": "Frieren: Beyond Journey's End",
                "image": "https://example.com/season1.jpg",
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": "2023-09-29", "runtime": 24},
                    {"episode_number": 2, "air_date": "2023-10-06", "runtime": 24},
                    {"episode_number": 3, "air_date": "2023-10-13", "runtime": 24},
                ],
            },
        }

        with self.assertRaises(anime_migration.AnimeMigrationError):
            anime_migration.migrate_flat_anime_to_grouped(
                self.user,
                self.flat_item,
                Sources.TVDB.value,
            )

        self.flat_entry.refresh_from_db()
        self.assertIsNone(self.flat_entry.migrated_to_item)
        self.assertEqual(Episode.objects.count(), 0)
