"""Tests for episode library-bucket derivation and the stray-row cleanup."""

import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status

SEASON_METADATA = {
    "episodes": [{"episode_number": 1, "still_path": None}],
    "_tvdb_episode_image_map": {},  # present -> skips the TVDB provider call
}


class GetEpisodeItemBucketTests(TestCase):
    """Season.get_episode_item must never bucket an episode as 'season'."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="test",
            password="12345",
        )

    def _make_season(self, library_media_type):
        item = Item.objects.create(
            media_id="63404",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=library_media_type,
            title="Taskmaster",
            image="http://example.com/i.jpg",
            season_number=21,
        )
        return Season.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

    def test_normal_season_creates_episode_bucket(self):
        """A 'season'-bucketed season yields 'episode'-bucketed episodes."""
        season = self._make_season(MediaTypes.SEASON.value)
        item = season.get_episode_item(1, SEASON_METADATA)
        self.assertEqual(item.library_media_type, MediaTypes.EPISODE.value)

    def test_grouped_season_keeps_grouping_bucket(self):
        """A grouped ('tv') season keeps episodes in the 'tv' bucket."""
        season = self._make_season(MediaTypes.TV.value)
        item = season.get_episode_item(1, SEASON_METADATA)
        self.assertEqual(item.library_media_type, MediaTypes.TV.value)


class EpisodeSeasonBucketMigrationTests(TestCase):
    """The 0125 data migration re-buckets or merges stray rows."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="test",
            password="12345",
        )
        self.migration = importlib.import_module(
            "app.migrations.0125_fix_episode_season_library_bucket",
        )

    def _stray(self, episode_number):
        return Item.objects.create(
            media_id="63404",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.SEASON.value,
            title="Taskmaster",
            image="http://example.com/i.jpg",
            season_number=21,
            episode_number=episode_number,
        )

    def test_rebuckets_stray_without_canonical(self):
        """No canonical sibling -> stray is re-bucketed in place to 'episode'."""
        stray = self._stray(1)
        self.migration.fix_episode_season_buckets(apps, None)
        stray.refresh_from_db()
        self.assertEqual(stray.library_media_type, MediaTypes.EPISODE.value)

    def test_merges_stray_into_canonical(self):
        """Canonical sibling exists -> dependents move to it and stray is gone."""
        canonical = Item.objects.create(
            media_id="63404",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.EPISODE.value,
            title="Taskmaster",
            image="http://example.com/i.jpg",
            season_number=21,
            episode_number=2,
        )
        stray = self._stray(2)

        season_item = Item.objects.create(
            media_id="63404",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.SEASON.value,
            title="Taskmaster",
            image="http://example.com/i.jpg",
            season_number=21,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        episode = Episode.objects.create(item=stray, related_season=season)

        self.migration.fix_episode_season_buckets(apps, None)

        self.assertFalse(Item.objects.filter(pk=stray.pk).exists())
        episode.refresh_from_db()
        self.assertEqual(episode.item_id, canonical.pk)

    def test_orphan_stray_with_canonical_is_deleted(self):
        """An orphan stray with a canonical sibling is simply removed."""
        Item.objects.create(
            media_id="63404",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.EPISODE.value,
            title="Taskmaster",
            image="http://example.com/i.jpg",
            season_number=21,
            episode_number=9,
        )
        stray = self._stray(9)

        self.migration.fix_episode_season_buckets(apps, None)

        self.assertFalse(Item.objects.filter(pk=stray.pk).exists())
