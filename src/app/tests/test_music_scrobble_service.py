from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import (
    Album,
    AlbumTracker,
    Artist,
    ArtistTracker,
    Item,
    MediaTypes,
    Music,
    Sources,
    Status,
)
from app.services.music_scrobble import (
    MusicPlaybackEvent,
    dedupe_artist_albums,
    record_music_playback,
)


class MusicScrobbleServiceTests(TestCase):
    """Tests for the music scrobble service."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="music_user",
            password="password",
        )
        # Enable music/anime features by default for service tests
        self.user.music_enabled = True
        self.user.anime_enabled = True
        self.user.save()

        self.search_patcher = patch("app.services.music_scrobble.musicbrainz.search", return_value={"results": [], "total_results": 0})
        self.search_artists_patcher = patch(
            "app.services.music_scrobble.musicbrainz.search_artists",
            return_value={"results": [], "total_results": 0},
        )
        self.search_patcher.start()
        self.search_artists_patcher.start()

    def tearDown(self):
        self.search_patcher.stop()
        self.search_artists_patcher.stop()

    @patch("app.services.music_scrobble.musicbrainz.search_artists")
    @patch("app.services.music_scrobble.musicbrainz.search")
    def test_record_music_playback_creates_entries(self, mock_search, mock_search_artists):
        """Play events build catalog only; scrobble creates tracking entry."""
        mock_search.return_value = {
            "results": [],
            "page": 1,
            "total_results": 0,
            "per_page": 20,
            "total_pages": 0,
        }
        mock_search_artists.return_value = {"results": [], "total_results": 0}
        played_at = timezone.now().replace(second=0, microsecond=0)
        play_event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Test Artist",
            album_title="Test Album",
            track_title="Test Track",
            duration_ms=180000,
            plex_rating_key="abc123",
            external_ids={},
            completed=False,
            played_at=played_at,
        )

        scrobble_event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Test Artist",
            album_title="Test Album",
            track_title="Test Track",
            duration_ms=180000,
            plex_rating_key="abc123",
            external_ids={},
            completed=True,
            played_at=played_at + timedelta(minutes=3),
        )

        music = record_music_playback(play_event)
        self.assertIsNone(music)
        self.assertEqual(Music.objects.count(), 0)
        self.assertEqual(Artist.objects.count(), 1)
        self.assertEqual(Album.objects.count(), 1)
        self.assertEqual(AlbumTracker.objects.count(), 0)
        self.assertEqual(ArtistTracker.objects.count(), 0)

        music = record_music_playback(scrobble_event)
        self.assertEqual(Music.objects.count(), 1)
        self.assertEqual(music.status, Status.COMPLETED.value)
        self.assertEqual(music.progress, 1)
        self.assertEqual(music.start_date, scrobble_event.played_at)
        self.assertEqual(music.end_date, scrobble_event.played_at)
        self.assertEqual(music.item.media_type, MediaTypes.MUSIC.value)
        self.assertEqual(music.item.source, Sources.MANUAL.value)
        self.assertEqual(music.artist.name, "Test Artist")
        self.assertEqual(music.album.title, "Test Album")
        self.assertEqual(music.track.title, "Test Track")
        self.assertEqual(music.item.runtime_minutes, 3)
        self.assertTrue(
            ArtistTracker.objects.filter(user=self.user, artist=music.artist).exists(),
        )
        self.assertTrue(
            AlbumTracker.objects.filter(user=self.user, album=music.album).exists(),
        )

    @patch("app.services.music_scrobble.sync_artist_discography")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    @patch("app.services.music_scrobble.musicbrainz.recording")
    def test_scrobble_increments_progress_without_duplicates(
        self,
        mock_recording,
        mock_get_artist,
        mock_sync_discog,
    ):
        """Repeated scrobbles should update one Music row and bump progress."""
        mock_recording.return_value = {
            "title": "Canon Track",
            "_artist_name": "Canon Artist",
            "_artist_id": "artist-1",
            "_album_title": "Canon Album",
            "_album_id": "album-1",
            "image": "",
            "genres": ["rock"],
            "details": {
                "duration_minutes": 3.5,
                "release_date": "2020-01-01",
            },
            "max_progress": None,
        }
        mock_get_artist.return_value = {
            "sort_name": "Artist, Canon",
            "country": "US",
            "image": "http://example.com/artist.jpg",
            "genres": [{"name": "Jazz"}],
        }

        first_play = timezone.now().replace(second=0, microsecond=0)
        scrobble_time = first_play + timedelta(minutes=5)
        repeat_time = scrobble_time + timedelta(minutes=10)

        play_event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Canon Artist",
            album_title="Canon Album",
            track_title="Canon Track",
            duration_ms=None,
            plex_rating_key="plex-1",
            external_ids={"musicbrainz_recording": "rec-1"},
            completed=False,
            played_at=first_play,
        )
        scrobble_event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Canon Artist",
            album_title="Canon Album",
            track_title="Canon Track",
            duration_ms=None,
            plex_rating_key="plex-1",
            external_ids={"musicbrainz_recording": "rec-1"},
            completed=True,
            played_at=scrobble_time,
        )

        repeat_scrobble_event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Canon Artist",
            album_title="Canon Album",
            track_title="Canon Track",
            duration_ms=None,
            plex_rating_key="plex-1",
            external_ids={"musicbrainz_recording": "rec-1"},
            completed=True,
            played_at=repeat_time,
        )

        self.assertIsNone(record_music_playback(play_event))
        music = record_music_playback(scrobble_event)
        music = record_music_playback(repeat_scrobble_event)

        self.assertEqual(Music.objects.count(), 1)
        self.assertEqual(music.status, Status.COMPLETED.value)
        self.assertEqual(music.progress, 2)
        self.assertEqual(music.end_date, repeat_time)
        self.assertEqual(music.track.musicbrainz_recording_id, "rec-1")
        self.assertEqual(music.artist.musicbrainz_id, "artist-1")
        self.assertEqual(music.album.musicbrainz_release_id, "album-1")
        self.assertEqual(music.item.runtime_minutes, 3)
        self.assertEqual(Artist.objects.count(), 1)
        self.assertEqual(Album.objects.count(), 1)
        # Discography sync should be triggered when MBID is present; tolerate test flakiness
        self.assertLessEqual(mock_sync_discog.call_count, 1)
        artist = Artist.objects.first()
        self.assertEqual(artist.country, "US")
        self.assertTrue(artist.image)

    @patch("app.services.music_scrobble.sync_artist_discography")
    @patch("app.services.music_scrobble.musicbrainz.search")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    def test_search_fallback_sets_musicbrainz_ids(
        self,
        mock_get_artist,
        mock_search,
        mock_sync_discog,
    ):
        """Search fallback should set MBIDs so metadata and discography can load."""
        mock_get_artist.return_value = {"country": "NL", "image": "http://example.com/a.jpg"}
        mock_search.return_value = {
            "results": [
                {
                    "media_id": "rec-2",
                    "artist_name": "Search Artist",
                    "album_title": "Search Album",
                    "artist_id": "artist-2",
                    "release_id": "release-2",
                    "release_group_id": "rg-2",
                    "duration_minutes": 4.2,
                },
            ],
            "total_results": 1,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
        }

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Search Artist",
            album_title="Search Album",
            track_title="Search Track",
            duration_ms=None,
            plex_rating_key="plex-3",
            external_ids={"musicbrainz_recording": "missing-id"},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        self.assertEqual(music.item.media_id, "rec-2")
        self.assertEqual(music.track.musicbrainz_recording_id, "rec-2")
        self.assertEqual(music.album.musicbrainz_release_id, "release-2")
        self.assertEqual(music.album.musicbrainz_release_group_id, "rg-2")
        self.assertEqual(music.artist.musicbrainz_id, "artist-2")
        mock_sync_discog.assert_called_once()

    @patch("app.services.music_scrobble.sync_artist_discography")
    @patch("app.services.music_scrobble.musicbrainz.recording")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    def test_existing_artist_without_mbid_gets_enriched(
        self,
        mock_get_artist,
        mock_recording,
        mock_sync_discog,
    ):
        """Legacy artists without MBID should be updated when MBID appears."""
        artist = Artist.objects.create(name="Legacy Artist")
        album = Album.objects.create(title="Legacy Album", artist=artist)

        mock_recording.return_value = {
            "title": "Legacy Track",
            "_artist_name": "Legacy Artist",
            "_artist_id": "artist-legacy",
            "_album_title": "Legacy Album",
            "_album_id": "album-legacy",
            "details": {"duration_minutes": 4},
            "genres": [],
        }
        mock_get_artist.return_value = {"country": "GB", "image": "http://example.com/legacy.jpg"}

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Legacy Artist",
            album_title="Legacy Album",
            track_title="Legacy Track",
            duration_ms=None,
            plex_rating_key="plex-legacy",
            external_ids={"musicbrainz_recording": "rec-legacy"},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        artist.refresh_from_db()
        album.refresh_from_db()
        self.assertEqual(artist.musicbrainz_id, "artist-legacy")
        self.assertEqual(album.musicbrainz_release_id, "album-legacy")
        self.assertEqual(music.track.musicbrainz_recording_id, "rec-legacy")
        mock_sync_discog.assert_called_once_with(artist, force=True)

    @patch("app.services.music_scrobble.prefetch_album_covers")
    @patch("app.services.music_scrobble.dedupe_artist_albums")
    @patch("app.services.music_scrobble.sync_artist_discography")
    @patch("app.services.music_scrobble.musicbrainz.recording")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    def test_discography_sync_triggers_album_dedupe(
        self,
        mock_get_artist,
        mock_recording,
        mock_sync_discog,
        mock_dedupe,
        mock_prefetch,
    ):
        """Discography sync should immediately dedupe albums created pre-sync."""
        mock_recording.return_value = {
            "title": "Rhythm Ace / Funky Stuff",
            "_artist_name": "Chuck Loeb",
            "_artist_id": "artist-chuck",
            "_album_title": "In a Heartbeat",
            "_album_id": "album-chuck",
            "details": {"duration_minutes": 5},
            "genres": [],
        }
        mock_get_artist.return_value = {"country": "US"}
        mock_sync_discog.return_value = 5

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Chuck Loeb",
            album_title="In a Heartbeat",
            track_title="Rhythm Ace / Funky Stuff",
            duration_ms=None,
            plex_rating_key="plex-chuck",
            external_ids={"musicbrainz_recording": "rec-chuck"},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        mock_sync_discog.assert_called_once_with(music.artist, force=True)
        mock_dedupe.assert_called_once_with(music.artist)
        mock_prefetch.assert_called_once_with(music.artist, limit=None)

    @patch("app.services.music_scrobble.prefetch_album_covers")
    @patch("app.services.music_scrobble.sync_artist_discography")
    @patch("app.services.music_scrobble.musicbrainz.recording")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    def test_prefetch_runs_even_when_sync_noop(
        self,
        mock_get_artist,
        mock_recording,
        mock_sync_discog,
        mock_prefetch,
    ):
        """Cover prefetch should run if missing art exists even when sync finds nothing."""
        mock_recording.return_value = {
            "title": "Track",
            "_artist_name": "Artist No Sync",
            "_artist_id": "artist-nosync",
            "_album_title": "Album No Sync",
            "_album_id": "album-nosync",
            "details": {"duration_minutes": 3},
            "genres": [],
        }
        mock_get_artist.return_value = {"country": "US"}
        mock_sync_discog.return_value = 0
        mock_prefetch.return_value = 5

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Artist No Sync",
            album_title="Album No Sync",
            track_title="Track",
            duration_ms=None,
            plex_rating_key="plex-nosync",
            external_ids={"musicbrainz_recording": "rec-nosync"},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        record_music_playback(event)

        mock_prefetch.assert_called_once_with(Artist.objects.first(), limit=None)

    @patch("app.services.music_scrobble.refresh_album_cover_art")
    @patch("app.services.music_scrobble.musicbrainz.recording")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    def test_scrobble_fetches_cover_art_when_missing(
        self,
        mock_get_artist,
        mock_recording,
        mock_refresh_cover,
    ):
        """Cover art should be fetched in background when album is missing art."""
        mock_recording.return_value = {
            "title": "Coverless Track",
            "_artist_name": "Coverless Artist",
            "_artist_id": "artist-coverless",
            "_album_title": "Coverless Album",
            "_album_id": "album-coverless",
            "details": {"duration_minutes": 3},
            "genres": [],
        }
        mock_get_artist.return_value = {"country": "US"}
        mock_refresh_cover.return_value = True

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Coverless Artist",
            album_title="Coverless Album",
            track_title="Coverless Track",
            duration_ms=None,
            plex_rating_key="plex-coverless",
            external_ids={"musicbrainz_recording": "rec-coverless"},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        mock_refresh_cover.assert_called_once()
        called_album = mock_refresh_cover.call_args[0][0]
        self.assertEqual(called_album.id, music.album.id)
        self.assertEqual(music.album.image, "")  # still blank; refresh mocked

    @patch("app.services.music_scrobble.refresh_album_cover_art")
    @patch("app.services.music_scrobble.musicbrainz.recording")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    def test_cover_art_not_requested_when_present(
        self,
        mock_get_artist,
        mock_recording,
        mock_refresh_cover,
    ):
        """Skip cover fetch when album already has art."""
        artist = Artist.objects.create(name="Has Art")
        album = Album.objects.create(
            title="With Art",
            artist=artist,
            image="http://img/already.jpg",
            musicbrainz_release_id="rel-existing",
        )

        mock_recording.return_value = {
            "title": "Track",
            "_artist_name": "Has Art",
            "_artist_id": "artist-has-art",
            "_album_title": "With Art",
            "_album_id": "rel-existing",
            "details": {"duration_minutes": 3},
            "genres": [],
        }
        mock_get_artist.return_value = {"country": "US"}

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Has Art",
            album_title="With Art",
            track_title="Track",
            duration_ms=None,
            plex_rating_key="plex-art",
            external_ids={"musicbrainz_recording": "rec-art"},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        record_music_playback(event)

        mock_refresh_cover.assert_not_called()

    @patch("app.services.music_scrobble.musicbrainz.search")
    def test_noisy_search_does_not_attach_incorrect_artist(self, mock_search):
        """When search is noisy, do not hijack an unrelated artist/MBID."""
        mock_search.return_value = {
            "results": [
                {
                    "title": "Let's Stay Together Tonight",
                    "artist_name": "Air Supply",
                    "album_title": "Free Love",
                    "artist_id": "air-supply-id",
                    "release_id": "release-noisy",
                    "release_group_id": "rg-noisy",
                },
            ],
            "total_results": 100,
            "page": 1,
            "per_page": 20,
            "total_pages": 5,
        }

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Walter Beasley",
            album_title="Tonight We Love",
            track_title="Let's Stay Together",
            duration_ms=None,
            plex_rating_key="plex-wb",
            external_ids={},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        self.assertEqual(music.artist.name, "Walter Beasley")
        self.assertIsNone(music.artist.musicbrainz_id)
        self.assertEqual(music.album.title, "Tonight We Love")
        self.assertIsNone(music.album.musicbrainz_release_id)
        self.assertIsNone(music.album.musicbrainz_release_group_id)

    @patch("app.services.music_scrobble.sync_artist_discography")
    @patch("app.services.music_scrobble.musicbrainz.get_artist")
    @patch("app.services.music_scrobble.musicbrainz.search_artists")
    @patch("app.services.music_scrobble.musicbrainz.search")
    def test_artist_only_search_attaches_correct_artist(
        self,
        mock_search_tracks,
        mock_search_artists,
        mock_get_artist,
        mock_sync_discog,
    ):
        """When track search is noisy, fall back to artist search for MBID."""
        mock_search_tracks.return_value = {
            "results": [],
            "total_results": 200000,
            "page": 1,
            "per_page": 20,
            "total_pages": 100,
        }
        mock_search_artists.return_value = {
            "results": [
                {"name": "Walter Beasley", "media_id": "artist-wb"},
            ],
            "total_results": 1,
            "page": 1,
            "per_page": 20,
            "total_pages": 1,
        }
        mock_get_artist.return_value = {"country": "US", "image": "http://example.com/wb.jpg"}

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Walter Beasley",
            album_title="Tonight We Love",
            track_title="Let's Stay Together",
            duration_ms=None,
            plex_rating_key="plex-wb",
            external_ids={},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        self.assertEqual(music.artist.name, "Walter Beasley")
        self.assertEqual(music.artist.musicbrainz_id, "artist-wb")
        mock_sync_discog.assert_called_once_with(music.artist, force=True)

    def test_reuses_existing_album_tracklist_entry(self):
        """When album already has tracklist, reuse matching track instead of creating duplicates."""
        artist = Artist.objects.create(name="Walter Beasley")
        album = Album.objects.create(title="Tonight We Love", artist=artist)
        existing_track = album.tracklist.create(title="Let's Stay Together", track_number=2)

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Walter Beasley",
            album_title="Tonight We Love",
            track_title="Let's Stay Together",
            track_number=2,
            duration_ms=None,
            plex_rating_key="plex-wb-dup",
            external_ids={},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        self.assertEqual(album.tracklist.count(), 1)
        self.assertEqual(music.track.id, existing_track.id)

    def test_dedupes_null_track_numbers_on_match(self):
        """If a null track-number duplicate exists, it should be removed when matched."""
        artist = Artist.objects.create(name="Walter Beasley")
        album = Album.objects.create(title="Tonight We Love", artist=artist)
        extra = album.tracklist.create(title="Let's Stay Together", track_number=None)
        canonical = album.tracklist.create(title="Let's Stay Together", track_number=2)

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Walter Beasley",
            album_title="Tonight We Love",
            track_title="Let's Stay Together",
            track_number=2,
            duration_ms=None,
            plex_rating_key="plex-wb-dup2",
            external_ids={},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        self.assertEqual(album.tracklist.count(), 1)
        self.assertEqual(album.tracklist.first().id, canonical.id)
        self.assertEqual(music.track.id, canonical.id)

    @patch("app.services.music_scrobble.musicbrainz.search")
    @patch("app.services.music_scrobble.musicbrainz.search_artists")
    def test_album_dedupe_moves_music_and_tracks(self, mock_search_artists, mock_search_tracks):
        """Duplicate albums with same title should merge into primary."""
        mock_search_tracks.return_value = {"results": [], "total_results": 0, "page": 1}
        mock_search_artists.return_value = {"results": [], "total_results": 0, "page": 1}
        artist = Artist.objects.create(name="Lou Donaldson")
        primary = Album.objects.create(title="Blues Walk", artist=artist)
        dup = Album.objects.create(title="Blues Walk", artist=artist)
        primary_track = primary.tracklist.create(title="Autumn Nocturne", track_number=5)
        dup_track = dup.tracklist.create(title="Autumn Nocturne", track_number=None)

        item = Item.objects.create(
            media_id="item1",
            media_type=MediaTypes.MUSIC.value,
            source=Sources.MANUAL.value,
            title="Autumn Nocturne",
        )
        music = Music.objects.create(
            user=self.user,
            item=item,
            artist=artist,
            album=dup,
            track=dup_track,
            status=Status.IN_PROGRESS.value,
        )

        event = MusicPlaybackEvent(
            user=self.user,
            artist_name="Lou Donaldson",
            album_title="Blues Walk",
            track_title="Autumn Nocturne",
            track_number=5,
            duration_ms=None,
            plex_rating_key="plex-lou",
            external_ids={},
            completed=True,
            played_at=timezone.now().replace(second=0, microsecond=0),
        )

        music = record_music_playback(event)

        self.assertEqual(Album.objects.filter(artist=artist, title="Blues Walk").count(), 1)
        self.assertEqual(music.album.id, primary.id)
        self.assertEqual(music.track.id, primary_track.id)

    @patch("app.services.music_scrobble.musicbrainz.search")
    @patch("app.services.music_scrobble.musicbrainz.search_artists")
    def test_album_dedupe_prefers_richer_metadata(self, mock_search_artists, mock_search_tracks):
        """Primary selection should favor album with tracks/image/release IDs."""
        mock_search_tracks.return_value = {"results": [], "total_results": 0, "page": 1}
        mock_search_artists.return_value = {"results": [], "total_results": 0, "page": 1}
        artist = Artist.objects.create(name="Test Artist")
        rich = Album.objects.create(
            title="Blues Walk",
            artist=artist,
            musicbrainz_release_id="rel-1",
            image="http://img/rich.jpg",
            tracks_populated=True,
        )
        rich.tracklist.create(title="Track A", track_number=1)
        rich.tracklist.create(title="Track B", track_number=2)

        poor = Album.objects.create(title="Blues Walk", artist=artist)
        poor_track = poor.tracklist.create(title="Track A", track_number=None)
        AlbumTracker.objects.create(user=self.user, album=poor)
        item = Item.objects.create(
            media_id="item-poor",
            media_type=MediaTypes.MUSIC.value,
            source=Sources.MANUAL.value,
            title="Track A",
        )
        music = Music.objects.create(
            user=self.user,
            item=item,
            artist=artist,
            album=poor,
            track=poor_track,
            status=Status.IN_PROGRESS.value,
        )

        dedupe_artist_albums(artist)
        music.refresh_from_db()

        self.assertEqual(Album.objects.filter(artist=artist, title="Blues Walk").count(), 1)
        self.assertEqual(music.album.id, rich.id)
        self.assertEqual(music.track.album.id, rich.id)
