from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, tag

from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.services.grouped_anime import GroupedAnimeMatch
from integrations.webhooks.generic_scrobble import GenericScrobbleProcessor


class ResolveAnimeLiveIdentityTests(TestCase):
    """The live Now Playing card follows the same anidb->MAL cour decision."""

    def setUp(self):
        """A MAL-provider anime user and the processor under test."""
        self.processor = GenericScrobbleProcessor()
        self.user = get_user_model().objects.create_user(username="live-anime")
        self.user.anime_enabled = True
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save()
        self._ids = {
            "tmdb_id": "603",
            "imdb_id": None,
            "tvdb_id": "9350138",
            "anidb_id": "3651",
        }

    def _resolve(self, **overrides):
        with (
            patch(
                "integrations.webhooks.anime_mappings.fetch_mapping_data",
                return_value={"anidb:3651:R": {"mal:849": {"1-": "1-"}}},
            ),
            patch(
                "app.providers.mal.anime",
                return_value={
                    "title": "Suzumiya Haruhi no Yuutsu",
                    "image": "https://example.com/haruhi.jpg",
                    "max_progress": 14,
                },
            ),
        ):
            return self.processor.resolve_anime_live_identity(
                self.user,
                overrides.get("ids", self._ids),
                overrides.get("season_number", 1),
                overrides.get("episode_number", 1),
            )

    def test_flat_mal_user_gets_the_mapped_cour(self):
        """The card is pinned to the MAL entry, not the payload's tmdb show."""
        identity = self._resolve()
        self.assertEqual(identity["source"], Sources.MAL.value)
        self.assertEqual(identity["media_id"], "849")
        self.assertEqual(identity["episode_number"], 1)
        self.assertIsNone(identity["season_number"])
        self.assertEqual(identity["series_title"], "Suzumiya Haruhi no Yuutsu")
        self.assertEqual(identity["image"], "https://example.com/haruhi.jpg")

    def test_no_anidb_id_leaves_default_resolution(self):
        """Without an anidb id the default tmdb resolution is untouched."""
        self.assertIsNone(
            self._resolve(ids={**self._ids, "anidb_id": None}),
        )

    def test_grouped_provider_defers_to_the_normal_decision(self):
        """A TMDB Anime Provider keeps the grouped path; no MAL override."""
        self.user.anime_metadata_source_default = Sources.TMDB.value
        self.user.save(update_fields=["anime_metadata_source_default"])
        self.assertIsNone(self._resolve())

    def test_episode_past_the_cour_end_is_not_pinned(self):
        """An episode the MAL entry would refuse falls back to default routing."""
        self.assertIsNone(self._resolve(episode_number=99))

    def test_anime_disabled_user_is_ignored(self):
        """The override only applies when the anime library is enabled."""
        self.user.anime_enabled = False
        self.user.save(update_fields=["anime_enabled"])
        self.assertIsNone(self._resolve())


class GenericScrobbleHeuristicTests(TestCase):
    """Unit tests for GenericScrobbleProcessor's hook methods."""

    def setUp(self):
        """Instantiate the processor under test."""
        self.processor = GenericScrobbleProcessor()

    def test_get_media_type_translates_episode_to_tv(self):
        """The API's 'episode' vocabulary maps to the base class's TV routing."""
        self.assertEqual(
            self.processor._get_media_type({"media_type": "episode"}),
            MediaTypes.TV.value,
        )
        self.assertEqual(
            self.processor._get_media_type({"media_type": "movie"}),
            MediaTypes.MOVIE.value,
        )
        self.assertIsNone(
            self.processor._get_media_type({"media_type": "unknown"}),
        )

    def test_extract_external_ids_reads_normalized_ids(self):
        """Ids are read straight through from the already-normalized payload."""
        ids = self.processor._extract_external_ids(
            {"ids": {"tmdb": "603", "imdb": "tt0133093", "tvdb": None}},
        )
        self.assertEqual(
            ids,
            {
                "tmdb_id": "603",
                "imdb_id": "tt0133093",
                "tvdb_id": None,
                "anidb_id": None,
            },
        )

    def test_extract_external_ids_reads_anidb(self):
        """An anidb id is carried through for the anime routing path."""
        ids = self.processor._extract_external_ids(
            {"ids": {"tvdb": "9350138", "anidb": "3651"}},
        )
        self.assertEqual(ids["anidb_id"], "3651")

    def test_is_played_honors_explicit_completed_override(self):
        """An explicit 'completed' flag overrides the position/duration heuristic."""
        self.assertTrue(
            self.processor._is_played(
                {"completed": True, "position_seconds": 0, "duration_seconds": 5400},
            ),
        )
        self.assertFalse(
            self.processor._is_played(
                {
                    "completed": False,
                    "position_seconds": 5400,
                    "duration_seconds": 5400,
                },
            ),
        )

    def test_is_played_uses_buffer_when_duration_known(self):
        """Position within the scrobble buffer of duration counts as played."""
        self.assertTrue(
            self.processor._is_played(
                {"position_seconds": 5380, "duration_seconds": 5400},
            ),
        )
        self.assertFalse(
            self.processor._is_played(
                {"position_seconds": 100, "duration_seconds": 5400},
            ),
        )

    def test_is_played_uses_fallback_when_duration_missing(self):
        """Without duration, a long-enough position falls back to a fixed threshold."""
        self.assertTrue(
            self.processor._is_played({"position_seconds": 20 * 60}),
        )
        self.assertFalse(
            self.processor._is_played({"position_seconds": 60}),
        )

    def test_is_played_false_without_position(self):
        """No position data at all means not played."""
        self.assertFalse(self.processor._is_played({}))


class GenericScrobbleProcessPayloadTests(TestCase):
    """process_payload() end-to-end for movie and episode paths."""

    def setUp(self):
        """Create the user the scrobble events are attributed to."""
        self.user = get_user_model().objects.create_user(username="scrobble-user")
        self.processor = GenericScrobbleProcessor()

    def test_no_ids_is_a_noop(self):
        """A payload with no external ids never reaches TMDB resolution."""
        self.processor.process_payload(
            {"media_type": "movie", "ids": {}, "completed": True},
            self.user,
        )
        self.assertFalse(Movie.objects.filter(user=self.user).exists())

    @tag("network")
    def test_movie_stop_marks_completed(self):
        """A completed movie stop event creates a completed Movie instance."""
        self.processor.process_payload(
            {
                "media_type": "movie",
                "ids": {"tmdb": "603"},
                "title": "The Matrix",
                "completed": True,
            },
            self.user,
        )

        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    @tag("network")
    def test_movie_stop_without_completion_marks_in_progress(self):
        """A stop event without completion marks the movie in progress."""
        self.processor.process_payload(
            {
                "media_type": "movie",
                "ids": {"tmdb": "603"},
                "title": "The Matrix",
                "completed": False,
            },
            self.user,
        )

        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)
        self.assertEqual(movie.progress, 0)

    def test_movie_completion_prefers_pending_activity_row(self):
        """Scrobble completion upgrades Planning before older completed history."""
        item = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
        )
        prior_completed = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        planning = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        with (
            patch(
                "app.providers.tmdb.movie",
                return_value={"title": "The Matrix", "image": ""},
            ),
            patch("app.services.metadata_resolution.upsert_provider_links"),
            patch(
                "app.providers.services.get_media_metadata",
                return_value={"max_progress": 1},
            ),
            patch("app.models.Item.fetch_releases"),
        ):
            self.processor.process_payload(
                {
                    "media_type": "movie",
                    "ids": {"tmdb": "603"},
                    "completed": True,
                },
                self.user,
            )

        planning.refresh_from_db()
        prior_completed.refresh_from_db()
        self.assertEqual(planning.status, Status.COMPLETED.value)
        self.assertIsNotNone(planning.end_date)
        self.assertEqual(prior_completed.status, Status.COMPLETED.value)
        self.assertEqual(Movie.objects.filter(item=item, user=self.user).count(), 2)

    @tag("network")
    def test_episode_stop_marks_episode_played(self):
        """A completed episode stop event resolves the show via tvdb/imdb."""
        self.processor.process_payload(
            {
                "media_type": "episode",
                "ids": {"tvdb": "303821", "imdb": "tt0583459"},
                "series_title": "Friends",
                "season_number": 1,
                "episode_number": 1,
                "completed": True,
            },
            self.user,
        )

        tv = TV.objects.get(item__media_id="1668", user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(item__media_id="1668", item__season_number=1)
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        self.assertTrue(
            Episode.objects.filter(
                item__media_id="1668",
                item__season_number=1,
                item__episode_number=1,
            ).exists(),
        )

    def test_episode_stop_with_anidb_id_tracks_the_mapped_mal_cour(self):
        """An anidb id resolves the exact MAL entry without a TMDB round-trip.

        The user's Anime Provider is MAL, so a flat row is the shape their
        library uses; the anidb id only decides *which* cour.
        """
        self.user.anime_enabled = True
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save()

        with (
            patch(
                "integrations.webhooks.anime_mappings.fetch_mapping_data",
                return_value={"anidb:3651:R": {"mal:849": {"1-": "1-"}}},
            ),
            patch(
                "app.providers.mal.anime",
                return_value={
                    "title": "Suzumiya Haruhi no Yuutsu",
                    "image": "",
                    "max_progress": 14,
                },
            ),
            patch("app.services.metadata_resolution.upsert_provider_links"),
            patch(
                "integrations.webhooks.anime_mappings.find_entries_for_mal_id",
                return_value=[],
            ),
        ):
            self.processor.process_payload(
                {
                    "media_type": "episode",
                    "ids": {"tvdb": "9350138", "anidb": "3651"},
                    "series_title": "Suzumiya Haruhi no Yuutsu",
                    "season_number": 1,
                    "episode_number": 1,
                    "completed": True,
                },
                self.user,
            )

        anime = Anime.objects.get(item__media_id="849", user=self.user)
        self.assertEqual(anime.item.source, Sources.MAL.value)
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertEqual(anime.progress, 1)
        # The MAL route is terminal: no parallel TV row is opened for the show.
        self.assertFalse(TV.objects.filter(user=self.user).exists())

    @patch("app.services.grouped_anime.classify_tv_metadata")
    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anidb_id_does_not_override_the_anime_provider_shape(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_fetch_mapping_data,
        mock_classify,
    ):
        """An anidb id names the cour; the Anime Provider decides the shape.

        Both routes are available: the anidb id maps to a MAL cour and the
        classifier returns a positive grouped verdict. Which one wins must be
        the user's Anime Provider, not the fact that the payload happened to
        carry an anidb id.
        """
        self.user.anime_enabled = True
        self.user.save()

        mock_find.return_value = {"tv_episode_results": [], "tv_results": []}
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "title": "Genesis Show",
            "image": "https://example.com/show.jpg",
            "tvdb_id": "402474",
            # Two episodes so marking the first does not complete the season
            # and fan out into a next-season metadata fetch.
            "season/1": {"episodes": [{"episode_number": 1}, {"episode_number": 2}]},
        }
        mock_fetch_mapping_data.return_value = {
            "anidb:3651:R": {"mal:46569": {"1-": "1-"}},
        }
        mock_mal_anime.return_value = {
            "media_id": "46569",
            "title": "Genesis Show",
            "image": "https://example.com/anime.jpg",
            "max_progress": 12,
        }
        mock_classify.return_value = GroupedAnimeMatch(
            decision="move",
            reason="exact_external_id_match",
            tmdb_id="12345",
            tvdb_id="402474",
            mal_ids=("46569",),
        )

        payload = {
            "media_type": "episode",
            "ids": {"tmdb": "12345", "tvdb": "402474", "anidb": "3651"},
            "series_title": "Genesis Show",
            "season_number": 1,
            "episode_number": 1,
            "completed": True,
        }

        for provider, expected_shape in (
            (Sources.MAL.value, "flat"),
            (Sources.TMDB.value, "grouped"),
        ):
            with self.subTest(provider=provider):
                Episode.objects.all().delete()
                Season.objects.all().delete()
                TV.objects.all().delete()
                Anime.objects.all().delete()
                Item.objects.all().delete()

                self.user.anime_metadata_source_default = provider
                self.user.save(update_fields=["anime_metadata_source_default"])

                with patch("app.services.metadata_resolution.upsert_provider_links"):
                    self.processor.process_payload(payload, self.user)

                if expected_shape == "flat":
                    self.assertEqual(Anime.objects.filter(user=self.user).count(), 1)
                    self.assertEqual(TV.objects.filter(user=self.user).count(), 0)
                else:
                    self.assertEqual(
                        Anime.objects.filter(user=self.user).count(),
                        0,
                        "an anidb id forced a flat MAL row against the "
                        "user's Anime Provider",
                    )
                    tv = TV.objects.get(user=self.user)
                    self.assertEqual(
                        tv.item.library_media_type,
                        MediaTypes.ANIME.value,
                    )

    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anidb_id_does_not_open_a_flat_row_beside_a_grouped_home(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_fetch_mapping_data,
    ):
        """Routing stays sticky: a grouped home keeps every later episode.

        Without this the show accrues progress in both libraries, which is the
        duplication the "Repair duplicated anime libraries" task cleans up.
        """
        self.user.anime_enabled = True
        self.user.save()

        mock_find.return_value = {"tv_episode_results": [], "tv_results": []}
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "title": "Grouped Show",
            "image": "https://example.com/show.jpg",
            "tvdb_id": "402474",
            "season/1": {"episodes": [{"episode_number": 1}, {"episode_number": 2}]},
        }
        mock_fetch_mapping_data.return_value = {
            "anidb:3651:R": {"mal:46569": {"1-": "1-"}},
        }
        mock_mal_anime.return_value = {
            "media_id": "46569",
            "title": "Grouped Show",
            "image": "https://example.com/anime.jpg",
            "max_progress": 12,
        }

        grouped_item = Item.objects.create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Grouped Show",
            image="https://example.com/show.jpg",
        )
        TV.objects.create(
            item=grouped_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        with patch("app.services.metadata_resolution.upsert_provider_links"):
            self.processor.process_payload(
                {
                    "media_type": "episode",
                    "ids": {"tmdb": "12345", "tvdb": "402474", "anidb": "3651"},
                    "series_title": "Grouped Show",
                    "season_number": 1,
                    "episode_number": 2,
                    "completed": True,
                },
                self.user,
            )

        self.assertEqual(
            Anime.objects.filter(user=self.user).count(),
            0,
            "an anidb id opened a flat MAL row beside an existing grouped home",
        )
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        self.assertEqual(
            TV.objects.get(user=self.user).item_id,
            grouped_item.id,
        )

    @patch("app.services.grouped_anime.classify_tv_metadata")
    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anidb_episode_refused_by_mal_is_not_dropped_silently(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_fetch_mapping_data,
        mock_classify,
    ):
        """An episode past the MAL cour's end falls through instead of vanishing.

        `_handle_anime` returns a sentinel here, not a boolean; treating it as
        success dropped the scrobble with no warning and no second chance.
        """
        self.user.anime_enabled = True
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save()

        mock_find.return_value = {"tv_episode_results": [], "tv_results": []}
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "title": "Split Cour Show",
            "image": "https://example.com/show.jpg",
            "tvdb_id": "402474",
            "season/1": {
                "episodes": [{"episode_number": n} for n in range(1, 15)],
            },
        }
        mock_fetch_mapping_data.return_value = {
            "anidb:3651:R": {"mal:46569": {"1-": "1-"}},
        }
        # The first cour ends at 12, so episode 13 is past its end.
        mock_mal_anime.return_value = {
            "media_id": "46569",
            "title": "Split Cour Show",
            "image": "https://example.com/anime.jpg",
            "max_progress": 12,
        }
        mock_classify.return_value = GroupedAnimeMatch(
            decision="move",
            reason="exact_external_id_match",
            tmdb_id="12345",
            tvdb_id="402474",
            mal_ids=("46569",),
        )

        with patch("app.services.metadata_resolution.upsert_provider_links"):
            self.processor.process_payload(
                {
                    "media_type": "episode",
                    "ids": {"tmdb": "12345", "tvdb": "402474", "anidb": "3651"},
                    "series_title": "Split Cour Show",
                    "season_number": 1,
                    "episode_number": 13,
                    "completed": True,
                },
                self.user,
            )

        # No MAL entry accepted it, so the grouped fallback keeps it in the
        # Anime library rather than losing it.
        self.assertEqual(Anime.objects.filter(user=self.user).count(), 0)
        tv = TV.objects.get(user=self.user)
        self.assertEqual(tv.item.library_media_type, MediaTypes.ANIME.value)

    def test_movie_resolution_failure_propagates(self):
        """A provider failure during resolution is not swallowed here."""
        with (
            patch(
                "app.providers.tmdb.movie",
                side_effect=RuntimeError("tmdb down"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.processor.process_payload(
                {
                    "media_type": "movie",
                    "ids": {"tmdb": "603"},
                    "completed": True,
                },
                self.user,
            )

        self.assertFalse(
            Item.objects.filter(
                media_id="603",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            ).exists(),
        )
