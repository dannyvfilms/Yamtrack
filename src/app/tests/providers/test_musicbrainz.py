from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from app.providers import musicbrainz


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
