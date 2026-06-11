from unittest.mock import patch

import requests
from django.conf import settings
from django.test import SimpleTestCase

from app.providers import musicbrainz


class MusicBrainzReleaseTests(SimpleTestCase):
    """Test release metadata formatting."""

    def test_capitalize_genre_handles_plain_hyphenated_and_acronym_values(self):
        self.assertEqual(musicbrainz.capitalize_genre("krautrock"), "Krautrock")
        self.assertEqual(musicbrainz.capitalize_genre("post-industrial"), "Post-Industrial")
        self.assertEqual(musicbrainz.capitalize_genre("idm"), "IDM")
        self.assertEqual(musicbrainz.capitalize_genre("post-idm"), "Post-IDM")

    def test_get_release_returns_structured_artist_credits(self):
        """Release details should preserve individual artist credits."""
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz._mb_request") as mock_mb_request,
        ):
            mock_mb_request.return_value = {
                "title": "Shared Album",
                "date": "2024-01-15",
                "release-group": {"id": "release-group-mbid"},
                "artist-credit": [
                    {
                        "name": "Artist One",
                        "joinphrase": " & ",
                        "artist": {
                            "id": "artist-one-mbid",
                            "name": "Artist One",
                            "sort-name": "One, Artist",
                        },
                    },
                    {
                        "name": "Artist Two",
                        "joinphrase": "",
                        "artist": {
                            "id": "artist-two-mbid",
                            "name": "Artist Two",
                            "sort-name": "Two, Artist",
                        },
                    },
                ],
                "media": [],
            }

            data = musicbrainz.get_release("release-mbid", skip_cover_art=True)

        self.assertEqual(data["artist_name"], "Artist One & Artist Two")
        self.assertEqual(
            data["artist_credits"],
            [
                {
                    "artist_id": "artist-one-mbid",
                    "name": "Artist One",
                    "sort_name": "One, Artist",
                    "join_phrase": " & ",
                },
                {
                    "artist_id": "artist-two-mbid",
                    "name": "Artist Two",
                    "sort_name": "Two, Artist",
                    "join_phrase": "",
                },
            ],
        )

    def test_get_release_capitalizes_genres(self):
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz._mb_request") as mock_mb_request,
        ):
            mock_mb_request.return_value = {
                "title": "Genre Album",
                "date": "2024-01-15",
                "release-group": {"id": "release-group-mbid"},
                "artist-credit": [],
                "genres": [{"name": "post-idm"}, {"name": "krautrock"}],
                "media": [],
            }

            data = musicbrainz.get_release("release-mbid", skip_cover_art=True)

        self.assertEqual(data["genres"], ["Post-IDM", "Krautrock"])

    def test_get_release_group_genres_prefers_genres_then_tags(self):
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz._mb_request") as mock_mb_request,
        ):
            mock_mb_request.return_value = {
                "genres": [{"name": "post-rock"}],
                "tags": [{"name": "ignored"}],
            }

            data = musicbrainz.get_release_group_genres("rg-1")

        self.assertEqual(data, ["Post-Rock"])

    def test_get_genre_parents_walks_parent_chain_and_dedupes(self):
        def _mock_genre_request(endpoint, params=None):
            if endpoint == "genre":
                return {"genres": [{"id": "dubstep-id", "name": "dubstep"}]}
            if endpoint == "genre/dubstep-id":
                return {
                    "relations": [
                        {
                            "type": "subgenre of",
                            "direction": "forward",
                            "genre": {"id": "edm-id", "name": "edm"},
                        },
                    ],
                }
            if endpoint == "genre/edm-id":
                return {
                    "relations": [
                        {
                            "type": "subgenre of",
                            "direction": "forward",
                            "genre": {"id": "electronic-id", "name": "electronic"},
                        },
                    ],
                }
            if endpoint == "genre/electronic-id":
                return {"relations": []}
            raise AssertionError(endpoint)

        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz._mb_request", side_effect=_mock_genre_request),
        ):
            data = musicbrainz.get_genre_parents("Dubstep")

        self.assertEqual(data, ["EDM", "Electronic"])

    def test_get_genre_parents_falls_back_when_direction_missing(self):
        def _mock_genre_request(endpoint, params=None):
            if endpoint == "genre":
                return {"genres": [{"id": "art-rock-id", "name": "art rock"}]}
            if endpoint == "genre/art-rock-id":
                return {
                    "relations": [
                        {
                            "type": "subgenre of",
                            "genre": {"id": "rock-id", "name": "rock"},
                        },
                    ],
                }
            if endpoint == "genre/rock-id":
                return {"relations": []}
            raise AssertionError(endpoint)

        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz._mb_request", side_effect=_mock_genre_request),
        ):
            data = musicbrainz.get_genre_parents("Art Rock")

        self.assertEqual(data, ["Rock"])

    def test_get_genre_parents_infers_parents_when_genre_search_is_unavailable(self):
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch(
                "app.providers.musicbrainz._mb_request",
                side_effect=requests.exceptions.HTTPError(
                    response=type("Response", (), {"status_code": 501})(),
                ),
            ),
        ):
            self.assertEqual(
                musicbrainz.get_genre_parents("Experimental Rock"),
                ["Experimental", "Rock"],
            )
            self.assertEqual(
                musicbrainz.get_genre_parents("Plunderphonics"),
                ["Experimental"],
            )

    def test_get_genre_parents_caches_negative_lookup(self):
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set") as mock_cache_set,
            patch("app.providers.musicbrainz._mb_request", return_value={"genres": []}) as mock_mb_request,
        ):
            data = musicbrainz.get_genre_parents("Missing Genre")

        self.assertEqual(data, [])
        self.assertEqual(mock_mb_request.call_count, 1)
        cache_values = [call.args[1] for call in mock_cache_set.call_args_list]
        self.assertIn("", cache_values)
        self.assertIn([], cache_values)


class MusicBrainzCombinedSearchTests(SimpleTestCase):
    """Test combined music search result formatting."""

    def test_page_one_uses_release_artwork_for_artists(self):
        """First page should populate artist artwork from matching release art."""
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz.search_artists") as mock_search_artists,
            patch("app.providers.musicbrainz.search_releases") as mock_search_releases,
            patch("app.providers.musicbrainz.search") as mock_search_tracks,
        ):
            mock_search_artists.return_value = {
                "results": [
                    {"artist_id": "artist-1", "name": "Pentatonix"},
                    {"artist_id": "artist-2", "name": "No Cover Artist"},
                ],
            }
            mock_search_releases.return_value = {
                "results": [
                    {
                        "release_id": "release-1",
                        "artist_id": "artist-1",
                        "artist_name": "Pentatonix",
                        "image": "http://example.com/cover.jpg",
                    },
                ],
            }
            mock_search_tracks.return_value = {
                "page": 1,
                "total_results": 0,
                "total_pages": 0,
                "results": [],
            }

            data = musicbrainz.search_combined("pentatonix", page=1)

            mock_search_artists.assert_called_once_with("pentatonix", page=1)
            mock_search_releases.assert_called_once_with(
                "pentatonix",
                page=1,
                skip_cover_art=True,
            )
            mock_search_tracks.assert_called_once_with(
                "pentatonix",
                page=1,
                skip_cover_art=True,
            )
            self.assertEqual(data["artists"][0]["image"], "http://example.com/cover.jpg")
            self.assertEqual(data["artists"][1]["image"], settings.IMG_NONE)
            self.assertEqual(data["releases"][0]["image"], "http://example.com/cover.jpg")

    def test_page_one_builds_async_cover_url_when_cover_missing(self):
        """First page should provide async cover URLs when no art is preloaded."""
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz.search_artists") as mock_search_artists,
            patch("app.providers.musicbrainz.search_releases") as mock_search_releases,
            patch("app.providers.musicbrainz.search") as mock_search_tracks,
        ):
            mock_search_artists.return_value = {
                "results": [{"artist_id": "artist-1", "name": "Pentatonix"}],
            }
            mock_search_releases.return_value = {
                "results": [
                    {
                        "release_id": "release-1",
                        "artist_id": "artist-1",
                        "artist_name": "Pentatonix",
                        "image": settings.IMG_NONE,
                    },
                ],
            }
            mock_search_tracks.return_value = {
                "page": 1,
                "total_results": 0,
                "total_pages": 0,
                "results": [],
            }

            data = musicbrainz.search_combined("pentatonix", page=1)

            expected_cover = (
                f"{musicbrainz.COVER_ART_BASE}/release/release-1/front-250"
            )
            self.assertEqual(data["releases"][0]["image"], expected_cover)
            self.assertEqual(data["artists"][0]["image"], expected_cover)

    def test_page_two_returns_tracks_only(self):
        """Subsequent pages should skip artist/album sections."""
        with (
            patch("app.providers.musicbrainz.cache.get", return_value=None),
            patch("app.providers.musicbrainz.cache.set"),
            patch("app.providers.musicbrainz.search_artists") as mock_search_artists,
            patch("app.providers.musicbrainz.search_releases") as mock_search_releases,
            patch("app.providers.musicbrainz.search") as mock_search_tracks,
        ):
            mock_search_tracks.return_value = {
                "page": 2,
                "total_results": 40,
                "total_pages": 2,
                "results": [{"media_id": "rec-1"}],
            }

            data = musicbrainz.search_combined("pentatonix", page=2)

            mock_search_artists.assert_not_called()
            mock_search_releases.assert_not_called()
            mock_search_tracks.assert_called_once_with(
                "pentatonix",
                page=2,
                skip_cover_art=True,
            )
            self.assertEqual(data["artists"], [])
            self.assertEqual(data["releases"], [])
            self.assertEqual(data["tracks"]["page"], 2)
