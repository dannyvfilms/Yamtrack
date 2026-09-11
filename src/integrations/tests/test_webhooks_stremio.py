import datetime
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.services.grouped_anime import GroupedAnimeMatch
from integrations.webhooks.stremio import StremioWebhookProcessor
from lists.models import CustomList, CustomListItem


class StremioAddonViewTests(TestCase):
    """Tests for the Stremio scrobbler addon endpoints."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "testuser", "token": "test-token"}
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        cache.clear()

    def _subtitles_url(self, media_type, media_id, token="test-token"):  # noqa: S107
        return f"/stremio-addon/{token}/subtitles/{media_type}/{media_id}.json"

    def _catalog_url(
        self,
        media_type="movie",
        catalog_id="floppy-watchlist-movies",
        extra=None,
        token="test-token",  # noqa: S107
    ):
        url = f"/stremio-addon/{token}/catalog/{media_type}/{catalog_id}"
        if extra is not None:
            url += f"/{extra}"
        return f"{url}.json"

    def _add_catalog_item(
        self,
        custom_list,
        index,
        *,
        media_type=MediaTypes.MOVIE.value,
        source=Sources.IMDB.value,
        media_id=None,
        provider_external_ids=None,
    ):
        item = Item.objects.create(
            title=f"Item {index}",
            media_id=media_id or f"tt{index + 1:07d}",
            media_type=media_type,
            source=source,
            provider_external_ids=provider_external_ids or {},
        )
        CustomListItem.objects.create(custom_list=custom_list, item=item)
        return item

    def _add_series_with_season(
        self,
        custom_list,
        index,
        *,
        show_media_id=None,
        show_source=Sources.IMDB.value,
        show_external_ids=None,
        season_release_date=None,
        season_number=1,
    ):
        show_item = Item.objects.create(
            title=f"Show {index}",
            media_id=show_media_id or f"tt{index + 1:07d}",
            media_type=MediaTypes.TV.value,
            source=show_source,
            provider_external_ids=show_external_ids or {},
            image=f"https://example.com/show{index}.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        season_item = Item.objects.create(
            title=f"Show {index} Season {season_number}",
            media_id=str(1000 + index),
            media_type=MediaTypes.SEASON.value,
            source=Sources.TMDB.value,
            season_number=season_number,
            release_datetime=season_release_date,
            image=f"https://example.com/season{index}.jpg",
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        CustomListItem.objects.get_or_create(custom_list=custom_list, item=season_item)
        return show_item, season_item

    def _response_metas(self, response):
        return json.loads(response.content)["metas"]

    def test_manifest(self):
        """The manifest is served with CORS headers."""
        url = reverse("stremio_addon_manifest", kwargs={"token": "test-token"})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        manifest = json.loads(response.content)
        self.assertEqual(manifest["id"], "org.yamtrack.scrobbler")
        self.assertEqual(manifest["resources"], ["catalog", "meta", "subtitles"])
        self.assertEqual(manifest["idPrefixes"], ["tt"])
        self.assertEqual(
            manifest["catalogs"],
            [
                {
                    "type": "movie",
                    "id": "floppy-watchlist-movies",
                    "name": "Floppy: Movies",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
                {
                    "type": "series",
                    "id": "floppy-watchlist-series",
                    "name": "Floppy: Series",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
                {
                    "type": "movie",
                    "id": "floppy-history-movies",
                    "name": "Floppy: History",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
                {
                    "type": "series",
                    "id": "floppy-history-series",
                    "name": "Floppy: History",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
                {
                    "type": "movie",
                    "id": "floppy-in-progress-movies",
                    "name": "Floppy: In Progress",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
                {
                    "type": "series",
                    "id": "floppy-in-progress-series",
                    "name": "Floppy: In Progress",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
            ],
        )
        self.assertEqual(response["Content-Type"], "application/json")

    def test_manifest_names_follow_selected_owned_sources(self):
        """Manifest names use the same owned-list rule as catalog projection."""
        CustomList.objects.create(name="Watchlist", owner=self.user)

        response = self.client.get(
            reverse("stremio_addon_manifest", kwargs={"token": "test-token"})
        )

        names = [catalog["name"] for catalog in response.json()["catalogs"]]
        self.assertEqual(
            names,
            [
                "Floppy: Watchlist",
                "Floppy: Watchlist",
                "Floppy: History",
                "Floppy: History",
                "Floppy: In Progress",
                "Floppy: In Progress",
            ],
        )

    def test_manifest_invalid_token(self):
        """An unknown token returns 401."""
        url = reverse("stremio_addon_manifest", kwargs={"token": "bad-token"})
        response = self.client.get(url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Content-Type"], "application/json")

    def test_catalog_pagination_matrix(self):
        """Catalog pages cover every requested boundary and never exceed 100 metas."""
        movies = CustomList.objects.create(name="Movies", owner=self.user)

        for count in (0, 1, 99, 100, 101, 250):
            with self.subTest(count=count):
                CustomListItem.objects.filter(custom_list=movies).delete()
                Item.objects.all().delete()
                for index in range(count):
                    self._add_catalog_item(movies, index)

                expected_ids = [f"tt{index + 1:07d}" for index in range(count)]
                expected_ids.reverse()
                for skip in (0, 100, 200):
                    response = self.client.get(
                        self._catalog_url(extra=f"skip={skip}")
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Access-Control-Allow-Origin"], "*")
                    self.assertEqual(response["Content-Type"], "application/json")
                    metas = self._response_metas(response)
                    self.assertLessEqual(len(metas), 100)
                    self.assertEqual(
                        [meta["id"] for meta in metas],
                        expected_ids[skip : skip + 100],
                    )

    def test_skip_applies_after_unresolved_items_are_filtered(self):
        """Unresolved rows do not consume skip positions or page capacity."""
        movies = CustomList.objects.create(name="Movies", owner=self.user)
        for index in range(102):
            self._add_catalog_item(movies, index)
            self._add_catalog_item(
                movies,
                1000 + index,
                source=Sources.TMDB.value,
                media_id=str(index),
            )

        first_page = self._response_metas(self.client.get(self._catalog_url()))
        second_page = self._response_metas(
            self.client.get(self._catalog_url(extra="skip=100"))
        )

        self.assertEqual(len(first_page), 100)
        self.assertEqual(len(second_page), 2)
        self.assertEqual(
            {meta["id"] for meta in first_page + second_page},
            {f"tt{index + 1:07d}" for index in range(102)},
        )

    def test_catalog_rejects_malformed_or_negative_skip(self):
        """Only one non-negative decimal skip argument is accepted."""
        for extra in ("skip=bad", "skip=-1", "skip=", "skip=1&skip=2", "genre=x"):
            with self.subTest(extra=extra):
                response = self.client.get(self._catalog_url(extra=extra))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response["Access-Control-Allow-Origin"], "*")
                self.assertEqual(response["Content-Type"], "application/json")

    def test_catalog_rejects_skip_exceeding_integer_conversion_limit(self):
        """Oversized decimal skip values return the catalog validation response."""
        response = self.client.get(self._catalog_url(extra=f"skip={'1' * 5000}"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertIsInstance(response.json(), dict)

    def test_catalog_ordering_is_deterministic_for_tied_dates(self):
        """Membership id is the stable tie-breaker for equal added dates."""
        movies = CustomList.objects.create(name="Movies", owner=self.user)
        items = [self._add_catalog_item(movies, index) for index in range(3)]
        CustomListItem.objects.filter(custom_list=movies).update(
            date_added=timezone.now()
        )

        metas = self._response_metas(self.client.get(self._catalog_url()))

        self.assertEqual(
            [meta["id"] for meta in metas],
            [item.media_id for item in reversed(items)],
        )

    @patch("app.providers.services.get_media_metadata")
    def test_catalog_resolves_only_local_imdb_ids(self, mock_metadata):
        """Direct and persisted IMDb ids publish without calling a provider."""
        movies = CustomList.objects.create(name="Movies", owner=self.user)
        direct = self._add_catalog_item(movies, 1, media_id="tt1234567")
        external = self._add_catalog_item(
            movies,
            2,
            source=Sources.TMDB.value,
            media_id="42",
            provider_external_ids={"imdb_id": "tt7654321"},
        )
        self._add_catalog_item(
            movies,
            3,
            source=Sources.IMDB.value,
            media_id="invalid",
        )
        self._add_catalog_item(
            movies,
            4,
            source=Sources.TMDB.value,
            media_id="43",
            provider_external_ids={"imdb_id": "not-imdb"},
        )

        metas = self._response_metas(self.client.get(self._catalog_url()))

        self.assertEqual(
            {meta["id"] for meta in metas},
            {direct.media_id, external.provider_external_ids["imdb_id"]},
        )
        mock_metadata.assert_not_called()

    @patch(
        "app.providers.services.get_media_metadata",
        side_effect=RuntimeError("provider unavailable"),
    )
    def test_catalog_read_is_offline_and_does_not_mutate_database(self, mock_metadata):
        """Catalog reads remain successful and read-only while providers are down."""
        movies = CustomList.objects.create(name="Movies", owner=self.user)
        self._add_catalog_item(movies, 1)
        self._add_catalog_item(
            movies,
            2,
            source=Sources.TMDB.value,
            media_id="unresolved",
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self._catalog_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._response_metas(response)), 1)
        mock_metadata.assert_not_called()
        mutation_prefixes = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")
        self.assertFalse(
            any(
                query["sql"].lstrip().upper().startswith(mutation_prefixes)
                for query in queries.captured_queries
            )
        )

    def test_catalog_uses_oldest_owned_exact_name_and_ignores_collaborators(self):
        """Reserved collaborator lists and non-exact owned names never publish."""
        other_user = get_user_model().objects.create_user(username="other")
        collaborator_movies = CustomList.objects.create(
            name="Movies",
            owner=other_user,
        )
        collaborator_movies.collaborators.add(self.user)
        self._add_catalog_item(collaborator_movies, 1, media_id="tt1111111")
        CustomList.objects.create(name="My Movies", owner=self.user)
        watchlist = CustomList.objects.create(name="watchLIST", owner=self.user)
        fallback_item = self._add_catalog_item(
            watchlist,
            2,
            media_id="tt2222222",
        )

        fallback_metas = self._response_metas(self.client.get(self._catalog_url()))

        self.assertEqual(
            [meta["id"] for meta in fallback_metas],
            [fallback_item.media_id],
        )

        oldest_movies = CustomList.objects.create(name="mOVIES", owner=self.user)
        oldest_item = self._add_catalog_item(
            oldest_movies,
            3,
            media_id="tt3333333",
        )
        newer_movies = CustomList.objects.create(name="Movies", owner=self.user)
        self._add_catalog_item(newer_movies, 4, media_id="tt4444444")

        preferred_metas = self._response_metas(self.client.get(self._catalog_url()))
        self.assertEqual(
            [meta["id"] for meta in preferred_metas],
            [oldest_item.media_id],
        )

    def test_collaborator_reserved_lists_never_publish(self):
        """Movies, Series, and Watchlist collaborator lists are all excluded."""
        other_user = get_user_model().objects.create_user(username="collaborator-owner")
        for index, name in enumerate(("Movies", "Series", "Watchlist")):
            custom_list = CustomList.objects.create(name=name, owner=other_user)
            custom_list.collaborators.add(self.user)
            self._add_catalog_item(
                custom_list,
                index,
                media_type=(
                    MediaTypes.TV.value if name == "Series" else MediaTypes.MOVIE.value
                ),
            )

        movie_response = self.client.get(self._catalog_url())
        series_response = self.client.get(
            self._catalog_url(
                media_type="series",
                catalog_id="floppy-watchlist-series",
            )
        )

        self.assertEqual(self._response_metas(movie_response), [])
        self.assertEqual(self._response_metas(series_response), [])

    def test_series_catalog_uses_owned_series_list(self):
        """The series catalog applies its independent owned-list and media rule."""
        series = CustomList.objects.create(name="Series", owner=self.user)
        tv_item = self._add_catalog_item(
            series,
            1,
            media_type=MediaTypes.TV.value,
        )
        self._add_catalog_item(series, 2, media_type=MediaTypes.MOVIE.value)

        response = self.client.get(
            self._catalog_url(
                media_type="series",
                catalog_id="floppy-watchlist-series",
            )
        )

        self.assertEqual(
            [meta["id"] for meta in self._response_metas(response)],
            [tv_item.media_id],
        )

    def test_series_catalog_projects_smart_seasons_as_parent_shows(self):
        """Season-only smart catalogs publish parent shows for Stremio."""
        series = CustomList.objects.create(
            name="Series",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.SEASON.value],
        )
        older_show, _ = self._add_series_with_season(
            series,
            1,
            season_release_date=datetime.datetime(2026, 1, 20, tzinfo=datetime.UTC),
        )
        newer_show, _ = self._add_series_with_season(
            series,
            2,
            season_release_date=datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC),
        )

        response = self.client.get(
            self._catalog_url(
                media_type="series",
                catalog_id="floppy-watchlist-series",
            )
        )

        self.assertEqual(
            [meta["id"] for meta in self._response_metas(response)],
            [newer_show.media_id, older_show.media_id],
        )
        self.assertEqual(
            [meta["name"] for meta in self._response_metas(response)],
            [newer_show.title, older_show.title],
        )

    def test_series_catalog_merges_smart_seasons_with_direct_tv_rows(self):
        """A mixed [tv, season] smart list keeps shows matched only directly.

        A show can match a smart list's rules through a direct TV row with no
        season in the list at all; dropping it there was issue #1101's bug.
        """
        series = CustomList.objects.create(
            name="Series",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.TV.value, MediaTypes.SEASON.value],
        )
        season_show, _ = self._add_series_with_season(
            series,
            1,
            season_release_date=datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC),
        )
        direct_tv = self._add_catalog_item(
            series,
            2,
            media_type=MediaTypes.TV.value,
            media_id="tt9999999",
        )

        response = self.client.get(
            self._catalog_url(
                media_type="series",
                catalog_id="floppy-watchlist-series",
            )
        )

        self.assertEqual(
            {meta["id"] for meta in self._response_metas(response)},
            {season_show.media_id, direct_tv.media_id},
        )

    def test_series_catalog_deduplicates_shows_by_newest_matching_season(self):
        """Multiple matching seasons produce one show at the newest season date."""
        series = CustomList.objects.create(
            name="Series",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.SEASON.value],
        )
        other_show, _ = self._add_series_with_season(
            series,
            1,
            season_release_date=datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC),
        )
        duplicate_show, _ = self._add_series_with_season(
            series,
            2,
            season_release_date=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            season_number=1,
        )
        duplicate_tv = TV.objects.get(item=duplicate_show, user=self.user)
        newest_duplicate_season = Item.objects.create(
            title="Show 2 Season 2",
            media_id="2002",
            media_type=MediaTypes.SEASON.value,
            source=Sources.TMDB.value,
            season_number=2,
            release_datetime=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        )
        Season.objects.create(
            item=newest_duplicate_season,
            user=self.user,
            related_tv=duplicate_tv,
            status=Status.PLANNING.value,
        )
        CustomListItem.objects.get_or_create(
            custom_list=series,
            item=newest_duplicate_season,
        )

        response = self.client.get(
            self._catalog_url(
                media_type="series",
                catalog_id="floppy-watchlist-series",
            )
        )

        self.assertEqual(
            [meta["id"] for meta in self._response_metas(response)],
            [duplicate_show.media_id, other_show.media_id],
        )

    def test_series_catalog_filters_unresolved_parent_shows_after_skip(self):
        """Unresolved season-derived parent shows do not consume skip slots."""
        series = CustomList.objects.create(
            name="Series",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.SEASON.value],
        )
        resolved_show, _ = self._add_series_with_season(
            series,
            1,
            season_release_date=datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC),
        )
        self._add_series_with_season(
            series,
            2,
            show_media_id="3002",
            show_source=Sources.TMDB.value,
            season_release_date=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        )

        first_page = self._response_metas(
            self.client.get(
                self._catalog_url(
                    media_type="series",
                    catalog_id="floppy-watchlist-series",
                )
            )
        )
        second_page = self._response_metas(
            self.client.get(
                self._catalog_url(
                    media_type="series",
                    catalog_id="floppy-watchlist-series",
                    extra="skip=1",
                )
            )
        )

        self.assertEqual(
            [meta["id"] for meta in first_page],
            [resolved_show.media_id],
        )
        self.assertEqual(second_page, [])

    def test_catalog_validates_token_and_catalog_with_json_cors_responses(self):
        """Token, catalog id, CORS, and JSON content type remain stable."""
        invalid_token = self.client.get(self._catalog_url(token="bad-token"))
        invalid_catalog = self.client.get(
            self._catalog_url(catalog_id="unknown-catalog")
        )
        mismatched_catalog = self.client.get(
            self._catalog_url(
                media_type="series",
                catalog_id="floppy-watchlist-movies",
            )
        )

        self.assertEqual(invalid_token.status_code, 401)
        self.assertEqual(invalid_token["Access-Control-Allow-Origin"], "*")
        self.assertEqual(invalid_token["Content-Type"], "application/json")
        self.assertEqual(invalid_catalog.status_code, 200)
        self.assertEqual(invalid_catalog.json(), {"metas": []})
        self.assertEqual(invalid_catalog["Access-Control-Allow-Origin"], "*")
        self.assertEqual(invalid_catalog["Content-Type"], "application/json")
        self.assertEqual(mismatched_catalog.status_code, 200)
        self.assertEqual(mismatched_catalog.json(), {"metas": []})

    def test_catalog_logs_one_unresolved_summary_per_page(self):
        """Unresolved items produce one aggregate log entry, not item warnings."""
        movies = CustomList.objects.create(name="Movies", owner=self.user)
        for index in range(3):
            self._add_catalog_item(
                movies,
                index,
                source=Sources.TMDB.value,
                media_id=str(index),
            )

        with self.assertLogs("integrations.views", level="INFO") as logs:
            response = self.client.get(self._catalog_url())

        self.assertEqual(response.status_code, 200)
        catalog_logs = [line for line in logs.output if "catalog projection" in line]
        self.assertEqual(len(catalog_logs), 1)
        self.assertIn("unresolved=3", catalog_logs[0])

    def test_subtitles_invalid_token(self):
        """An unknown token returns 401 on the subtitles resource."""
        response = self.client.get(
            self._subtitles_url("movie", "tt0133093", token="bad-token"),
        )

        self.assertEqual(response.status_code, 401)

    @patch("integrations.views.stremio_queue.reserve_pending", return_value="accepted")
    @patch("integrations.views.tasks.process_stremio_webhook.delay")
    def test_subtitles_enqueues_scrobble(self, mock_delay, _mock_reserve):
        """A subtitles request records one scrobble per throttle window."""
        response = self.client.get(self._subtitles_url("movie", "tt0133093"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(json.loads(response.content), {"subtitles": []})
        mock_delay.assert_called_once_with(
            {"id": "tt0133093", "type": "movie"},
            self.user.id,
            "movie:tt0133093",
        )

        # Repeat requests (seeks, quality changes) are throttled.
        self.client.get(self._subtitles_url("movie", "tt0133093"))
        self.assertEqual(mock_delay.call_count, 1)

        # A different item scrobbles immediately.
        self.client.get(self._subtitles_url("series", "tt0108778:1:1"))
        self.assertEqual(mock_delay.call_count, 2)

    @patch("integrations.views.stremio_queue.reserve_pending", return_value="accepted")
    @patch("integrations.views.tasks.process_stremio_webhook.delay")
    def test_subtitles_with_extra_path(self, mock_delay, _mock_reserve):
        """Stremio extra segments like videoHash are accepted and ignored."""
        response = self.client.get(
            "/stremio-addon/test-token/subtitles/series/"
            "tt0108778%3A1%3A1/videoHash=abcd1234.json",
        )

        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once_with(
            {"id": "tt0108778:1:1", "type": "series"},
            self.user.id,
            "series:tt0108778:1:1",
        )

    @patch("integrations.views.stremio_queue.reserve_pending", return_value="accepted")
    @patch("integrations.views.tasks.process_stremio_webhook.delay")
    def test_subtitles_with_slash_in_extra_path(self, mock_delay, _mock_reserve):
        """A release filename containing a slash still resolves and scrobbles."""
        response = self.client.get(
            "/stremio-addon/test-token/subtitles/movie/"
            "tt1872181/filename=Novyy Chelovek-pauk / The Amazing Spider-Man 2"
            " [2014].mp4&videoSize=60913373676.json",
        )
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once_with(
            {"id": "tt1872181", "type": "movie"},
            self.user.id,
            "movie:tt1872181",
        )

    @patch("integrations.views.stremio_queue.reserve_pending", return_value="limited")
    @patch("integrations.views.tasks.process_stremio_webhook.delay")
    def test_subtitles_limit_returns_empty_response_without_dispatch(
        self,
        mock_delay,
        _mock_reserve,
    ):
        """A full per-user queue remains a normal, fast subtitles response."""
        response = self.client.get(self._subtitles_url("series", "tt0133093:1:1"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"subtitles": []})
        mock_delay.assert_not_called()

    @patch("integrations.views.stremio_queue.reserve_pending", return_value="unavailable")
    @patch("integrations.views.tasks.process_stremio_webhook.delay")
    def test_subtitles_redis_failure_fails_closed(
        self,
        mock_delay,
        _mock_reserve,
    ):
        """Redis failure never falls back to unbounded direct task dispatch."""
        response = self.client.get(self._subtitles_url("movie", "tt0133093"))

        self.assertEqual(response.status_code, 200)
        mock_delay.assert_not_called()

    @patch("integrations.views.stremio_queue.reserve_pending")
    @patch("integrations.views.tasks.process_stremio_webhook.delay")
    def test_invalid_video_id_is_ignored(
        self,
        mock_delay,
        mock_reserve,
    ):
        """Zero/negative-style episode coordinates cannot enter the queue."""
        response = self.client.get(self._subtitles_url("series", "tt0133093:0:1"))

        self.assertEqual(response.status_code, 200)
        mock_reserve.assert_not_called()
        mock_delay.assert_not_called()


class StremioWebhookProcessorTests(TestCase):
    """Tests for the Stremio scrobble processor via the addon endpoint."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "testuser", "token": "test-token"}
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        cache.clear()

    def _get(self, media_type, media_id):
        return self.client.get(
            f"/stremio-addon/test-token/subtitles/{media_type}/{media_id}.json",
        )

    @patch.object(StremioWebhookProcessor, "_handle_tv_episode")
    @patch("app.services.grouped_anime.classify_tv_metadata")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch.object(StremioWebhookProcessor, "_find_tv_media_id")
    def test_exact_anime_match_routes_to_grouped_tv_episode(
        self,
        mock_find_tv,
        mock_tv_with_seasons,
        mock_classify,
        mock_handle_episode,
    ):
        """A matched Stremio series is routed to the grouped TV structure."""
        mock_find_tv.return_value = ("9001", None, None)
        mock_tv_with_seasons.return_value = {
            "title": "Anime Show",
            "image": "https://example.com/show.jpg",
            "tvdb_id": "7001",
            "provider_external_ids": {"imdb_id": "tt9001001"},
            "season/1": {
                "image": "https://example.com/season.jpg",
                "episodes": [{"episode_number": 1}],
            },
        }
        mock_classify.return_value = GroupedAnimeMatch(
            decision="move",
            reason="exact_external_id_and_animation_genre",
            tmdb_id="9001",
            tvdb_id="7001",
            mal_ids=("12345",),
        )

        processor = StremioWebhookProcessor()
        processor._process_tv(
            {"id": "tt9001001:1:1", "type": "series"},
            self.user,
            {"tmdb_id": None, "tvdb_id": None, "imdb_id": "tt9001001"},
            season_number=1,
            episode_number=1,
        )

        mock_handle_episode.assert_called_once_with(
            "9001",
            1,
            1,
            {"id": "tt9001001:1:1", "type": "series"},
            self.user,
            library_media_type="anime",
            grouped_anime_match=mock_classify.return_value,
        )

    @tag("network")
    def test_movie_start_marks_in_progress(self):
        """A movie playback start marks the movie in progress."""
        response = self._get("movie", "tt0133093")

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)
        self.assertEqual(movie.progress, 0)

    @tag("network")
    def test_episode_start_marks_show_in_progress(self):
        """An episode playback start marks the show and season in progress."""
        response = self._get("series", "tt0108778:1:1")

        self.assertEqual(response.status_code, 200)

        tv = TV.objects.get(item__media_id="1668", user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(
            item__media_id="1668",
            item__season_number=1,
        )
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        # Start-only signal: no completed episode record is created.
        self.assertFalse(
            Episode.objects.filter(item__media_id="1668").exists(),
        )

    def test_unsupported_id_is_ignored(self):
        """Non-IMDB ids are ignored without creating media."""
        response = self._get("movie", "yt%3Aabc123")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Movie.objects.filter(user=self.user).exists())

    @tag("network")
    @patch("integrations.webhooks.stremio.live_playback.apply_playback_event")
    def test_movie_start_updates_live_playback(self, mock_apply_event):
        """A movie playback start updates the Now Playing card."""
        response = self._get("movie", "tt0133093")

        self.assertEqual(response.status_code, 200)
        mock_apply_event.assert_called_once()
        _, kwargs = mock_apply_event.call_args
        self.assertEqual(kwargs["user_id"], self.user.id)
        self.assertEqual(kwargs["event_type"], "media.play")
        self.assertEqual(kwargs["playback_media_type"], "movie")
        self.assertEqual(kwargs["media_id"], "603")
        self.assertEqual(kwargs["title"], "The Matrix")

    @tag("network")
    @patch("integrations.webhooks.stremio.live_playback.apply_playback_event")
    def test_episode_start_updates_live_playback(self, mock_apply_event):
        """An episode playback start updates the Now Playing card."""
        response = self._get("series", "tt0108778:1:1")

        self.assertEqual(response.status_code, 200)
        mock_apply_event.assert_called_once()
        _, kwargs = mock_apply_event.call_args
        self.assertEqual(kwargs["user_id"], self.user.id)
        self.assertEqual(kwargs["event_type"], "media.play")
        self.assertEqual(kwargs["playback_media_type"], "episode")
        self.assertEqual(kwargs["media_id"], "1668")
        self.assertEqual(kwargs["series_title"], "Friends")
        self.assertEqual(kwargs["season_number"], 1)
        self.assertEqual(kwargs["episode_number"], 1)

    @patch("integrations.webhooks.stremio.live_playback.apply_playback_event")
    def test_unsupported_id_skips_live_playback(self, mock_apply_event):
        """Non-IMDB ids never reach the live playback update."""
        response = self._get("movie", "yt%3Aabc123")

        self.assertEqual(response.status_code, 200)
        mock_apply_event.assert_not_called()
