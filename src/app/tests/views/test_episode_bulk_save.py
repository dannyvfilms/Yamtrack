from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    PodcastShowTracker,
    Season,
    Sources,
    Status,
)
from app.services.metadata_resolution import MetadataResolutionResult


def _tv_base_payload(media_id, source, *, title="Test Show", seasons=None):
    return {
        "media_id": media_id,
        "title": title,
        "media_type": (
            MediaTypes.TV.value
            if source != Sources.MAL.value
            else MediaTypes.ANIME.value
        ),
        "source": source,
        "image": "https://example.com/show.jpg",
        "details": {
            "episodes": sum(len(season["episodes"]) for season in seasons or []),
        },
        "related": {
            "seasons": [
                {
                    "season_number": season["season_number"],
                    "season_title": season["season_title"],
                }
                for season in seasons or []
            ],
        },
    }


def _tv_with_seasons_payload(media_id, source, *, title="Test Show", seasons=None):
    payload = _tv_base_payload(media_id, source, title=title, seasons=seasons)
    for season in seasons or []:
        payload[f"season/{season['season_number']}"] = {
            "media_id": media_id,
            "source": source,
            "media_type": MediaTypes.SEASON.value,
            "title": title,
            "season_number": season["season_number"],
            "season_title": season["season_title"],
            "image": "https://example.com/season.jpg",
            "episodes": season["episodes"],
        }
    return payload


def _season_episode(episode_number, *, air_date):
    return {
        "episode_number": episode_number,
        "name": f"Episode {episode_number}",
        "air_date": air_date,
        "runtime": 24,
    }


@override_settings(TRACK_TIME=True)
class EpisodeBulkSaveViewTests(TestCase):
    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.return_url = "/details/tmdb/tv/1396/breaking-bad"

        self.default_resolution = MetadataResolutionResult(
            display_provider=Sources.TMDB.value,
            identity_provider=Sources.TMDB.value,
            mapping_status="identity",
            header_metadata={},
            grouped_preview=None,
            provider_media_id="1396",
        )

    def _post_bulk(self, data, *, next_url=None):
        return self.client.post(
            f"{reverse('episode_bulk_save')}?next={next_url or self.return_url}",
            data,
            HTTP_HX_REQUEST="true",
        )

    def test_podcast_bulk_add_creates_show_tracker_and_completed_entries(self):
        show = PodcastShow.objects.create(
            podcast_uuid="show-uuid-1",
            title="Podcast Show",
            image="https://example.com/show.jpg",
        )
        first_episode = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="pod-ep-1",
            title="Episode One",
            published=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
            duration=1800,
        )
        second_episode = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="pod-ep-2",
            title="Episode Two",
            published=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
            duration=2100,
        )

        response = self._post_bulk(
            {
                "media_id": show.podcast_uuid,
                "source": Sources.POCKETCASTS.value,
                "media_type": MediaTypes.PODCAST.value,
                "library_media_type": MediaTypes.PODCAST.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": "/details/pocketcasts/podcast/show-uuid-1/podcast-show",
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 2,
                "write_mode": "add",
                "distribution_mode": "air_date",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-03T00:00",
            },
            next_url="/details/pocketcasts/podcast/show-uuid-1/podcast-show",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            response["HX-Redirect"],
            "/details/pocketcasts/podcast/show-uuid-1/podcast-show",
        )
        self.assertTrue(
            PodcastShowTracker.objects.filter(user=self.user, show=show).exists(),
        )
        plays = list(
            Podcast.objects.filter(user=self.user, show=show)
            .select_related("episode")
            .order_by("end_date", "episode_id")
        )
        self.assertEqual(len(plays), 2)
        self.assertEqual(
            [play.episode_id for play in plays],
            [first_episode.id, second_episode.id],
        )
        self.assertTrue(all(play.status == Status.COMPLETED.value for play in plays))
        self.assertLess(plays[0].end_date, plays[1].end_date)
        self.assertTrue(
            Item.objects.filter(
                media_id="pod-ep-1",
                source=Sources.POCKETCASTS.value,
                media_type=MediaTypes.PODCAST.value,
            ).exists(),
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_bulk_add_creates_tracker_and_orders_same_day_timestamps(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date="2024-01-02"),
                    _season_episode(3, air_date="2024-01-03"),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 3,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-01T00:00",
            },
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], self.return_url)
        self.assertTrue(
            TV.objects.filter(user=self.user, item__media_id="1396").exists(),
        )
        self.assertEqual(
            Season.objects.filter(user=self.user, item__media_id="1396").count(),
            1,
        )
        episodes = list(
            Episode.objects.filter(
                related_season__user=self.user,
                item__media_id="1396",
            ).order_by("end_date", "item__episode_number")
        )
        self.assertEqual(len(episodes), 3)
        self.assertLess(episodes[0].end_date, episodes[1].end_date)
        self.assertLess(episodes[1].end_date, episodes[2].end_date)
        self.assertEqual(episodes[0].end_date.date(), episodes[2].end_date.date())

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_bulk_add_appends_existing_episode_plays(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date="2024-01-02"),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        tv_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Breaking Bad",
            image="https://example.com/season.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            image="https://example.com/ep1.jpg",
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        )

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": str(tv.id),
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 2,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-02T00:00",
            },
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=1,
            ).count(),
            2,
        )
        self.assertEqual(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=2,
            ).count(),
            1,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_replace_mode_preserves_out_of_range_plays(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date="2024-01-02"),
                    _season_episode(3, air_date="2024-01-03"),
                    _season_episode(4, air_date="2024-01-04"),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        tv_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Breaking Bad",
            image="https://example.com/season.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        for episode_number in (2, 3, 4):
            episode_item = Item.objects.create(
                media_id="1396",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=episode_number,
                title=f"Episode {episode_number}",
                image="https://example.com/episode.jpg",
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=datetime(2024, 1, episode_number, 0, 0, tzinfo=UTC),
            )
        extra_ep2 = Item.objects.get(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
        )
        Episode.objects.create(
            item=extra_ep2,
            related_season=season,
            end_date=datetime(2024, 1, 10, 0, 0, tzinfo=UTC),
        )

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": str(tv.id),
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 2,
                "last_season_number": 1,
                "last_episode_number": 3,
                "write_mode": "replace",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-02T00:00",
            },
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=2,
            ).count(),
            1,
        )
        self.assertEqual(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=3,
            ).count(),
            1,
        )
        self.assertEqual(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=4,
            ).count(),
            1,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_cross_season_range_uses_provider_order(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date="2024-01-02"),
                ],
            },
            {
                "season_number": 2,
                "season_title": "Season 2",
                "episodes": [
                    _season_episode(1, air_date="2024-02-01"),
                    _season_episode(2, air_date="2024-02-02"),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 2,
                "last_season_number": 2,
                "last_episode_number": 1,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-01T01:00",
            },
        )

        self.assertEqual(response.status_code, 204)
        created_pairs = list(
            Episode.objects.filter(
                related_season__user=self.user,
                item__media_id="1396",
            )
            .order_by("end_date")
            .values_list("item__season_number", "item__episode_number")
        )
        self.assertEqual(created_pairs, [(1, 2), (2, 1)])

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_air_date_mode_requires_date_range(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date="2024-01-02"),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 2,
                "write_mode": "add",
                "distribution_mode": "air_date",
                "start_date": "",
                "end_date": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start date is required.")
        self.assertContains(response, "End date is required.")
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_air_date_mode_scales_air_dates_into_selected_range(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date="2024-01-06"),
                    _season_episode(3, air_date="2024-01-11"),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 3,
                "write_mode": "add",
                "distribution_mode": "air_date",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-11T00:00",
            },
        )

        self.assertEqual(response.status_code, 204)
        episodes = list(
            Episode.objects.filter(
                related_season__user=self.user,
                item__media_id="1396",
            ).order_by("item__episode_number")
        )
        self.assertEqual(
            [episode.end_date.date().isoformat() for episode in episodes],
            [
                "2024-02-01",
                "2024-02-06",
                "2024-02-11",
            ],
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_air_date_mode_rejects_missing_air_dates(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        seasons = [
            {
                "season_number": 1,
                "season_title": "Season 1",
                "episodes": [
                    _season_episode(1, air_date="2024-01-01"),
                    _season_episode(2, air_date=None),
                ],
            },
        ]
        base_payload = _tv_base_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        tv_with_seasons = _tv_with_seasons_payload(
            "1396",
            Sources.TMDB.value,
            title="Breaking Bad",
            seasons=seasons,
        )
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else base_payload
        )
        mock_resolve_detail_metadata.return_value = self.default_resolution

        response = self._post_bulk(
            {
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.TV.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": self.return_url,
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 2,
                "write_mode": "add",
                "distribution_mode": "air_date",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-02T00:00",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "One or more selected episodes are missing air dates.",
        )
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_flat_mal_anime_bulk_save_creates_grouped_tracking_and_redirects(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "image": "https://example.com/frieren.jpg",
            "details": {"episodes": 2},
            "related": {},
        }
        grouped_base = {
            "media_id": "9350138",
            "title": "Frieren: Beyond Journey's End",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.TVDB.value,
            "image": "https://example.com/grouped.jpg",
            "details": {"episodes": 2},
            "related": {
                "seasons": [
                    {"season_number": 1, "season_title": "Season 1"},
                ],
            },
            "identity_media_type": MediaTypes.TV.value,
            "library_media_type": MediaTypes.ANIME.value,
        }
        grouped_preview = _tv_with_seasons_payload(
            "9350138",
            Sources.TVDB.value,
            title="Frieren: Beyond Journey's End",
            seasons=[
                {
                    "season_number": 1,
                    "season_title": "Season 1",
                    "episodes": [
                        _season_episode(1, air_date="2024-01-01"),
                        _season_episode(2, air_date="2024-01-02"),
                    ],
                },
            ],
        )

        def metadata_side_effect(media_type, _media_id, source, *_args, **_kwargs):
            if source == Sources.MAL.value:
                return base_metadata
            if media_type == "tv_with_seasons":
                return grouped_preview
            return grouped_base

        mock_get_metadata.side_effect = metadata_side_effect
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TVDB.value,
            identity_provider=Sources.MAL.value,
            mapping_status="mapped",
            header_metadata=base_metadata,
            grouped_preview=grouped_preview,
            provider_media_id="9350138",
            grouped_preview_target={
                "season_number": 1,
                "season_title": "Season 1",
                "episode_start": 1,
                "episode_end": 2,
            },
        )

        response = self._post_bulk(
            {
                "media_id": "52991",
                "source": Sources.MAL.value,
                "media_type": MediaTypes.ANIME.value,
                "library_media_type": "",
                "identity_media_type": "",
                "instance_id": "",
                "return_url": "/details/mal/anime/52991/frieren",
                "first_season_number": 1,
                "first_episode_number": 1,
                "last_season_number": 1,
                "last_episode_number": 2,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-02T00:00",
            },
            next_url="/details/mal/anime/52991/frieren",
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("/details/tvdb/anime/9350138/", response["HX-Redirect"])
        self.assertTrue(
            TV.objects.filter(
                user=self.user,
                item__media_id="9350138",
                item__library_media_type=MediaTypes.ANIME.value,
            ).exists(),
        )
        self.assertEqual(
            Episode.objects.filter(
                related_season__user=self.user,
                item__media_id="9350138",
            ).count(),
            2,
        )
