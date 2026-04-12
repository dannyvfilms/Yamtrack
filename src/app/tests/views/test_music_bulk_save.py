from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
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
    Track,
)


@override_settings(TRACK_TIME=True)
class MusicBulkSaveViewTests(TestCase):
    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def _album_detail_url(self, album):
        return reverse(
            "music_album_details",
            kwargs={
                "artist_id": album.artist.id,
                "artist_slug": album.artist.name.lower().replace(" ", "-"),
                "album_id": album.id,
                "album_slug": album.title.lower().replace(" ", "-"),
            },
        )

    def _artist_detail_url(self, artist):
        return reverse(
            "music_artist_details",
            kwargs={
                "artist_id": artist.id,
                "artist_slug": artist.name.lower().replace(" ", "-"),
            },
        )

    def test_album_track_modal_exposes_track_plays_tab_for_local_tracks(self):
        artist = Artist.objects.create(name="Test Artist")
        album = Album.objects.create(
            title="Test Album",
            artist=artist,
            release_date=date(2024, 1, 1),
            tracks_populated=True,
        )
        Track.objects.create(album=album, title="Track One", track_number=1, duration_ms=180000)
        Track.objects.create(album=album, title="Track Two", track_number=2, duration_ms=210000)

        response = self.client.get(
            reverse("album_track_modal", args=[album.id]) + "?return_url=/music",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["episode_plays_tab_available"])
        self.assertEqual(response.context["episode_plays_tab_label"], "Track Plays")
        self.assertContains(response, "Track Plays")
        self.assertContains(response, "Release date")

    def test_album_bulk_save_creates_music_entries_and_trackers(self):
        artist = Artist.objects.create(name="Bulk Artist")
        album = Album.objects.create(
            title="Bulk Album",
            artist=artist,
            release_date=date(2024, 1, 15),
            tracks_populated=True,
        )
        first_track = Track.objects.create(
            album=album,
            title="Intro",
            track_number=1,
            duration_ms=180000,
        )
        second_track = Track.objects.create(
            album=album,
            title="Middle",
            track_number=2,
            duration_ms=210000,
        )
        third_track = Track.objects.create(
            album=album,
            title="Finale",
            track_number=3,
            duration_ms=240000,
        )
        return_url = self._album_detail_url(album)

        response = self.client.post(
            f"{reverse('music_bulk_save')}?next={return_url}",
            {
                "media_id": str(album.id),
                "source": Sources.MUSICBRAINZ.value,
                "media_type": MediaTypes.MUSIC.value,
                "library_media_type": MediaTypes.MUSIC.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": return_url,
                "context_kind": "album",
                "context_id": str(album.id),
                "first_season_number": album.id,
                "first_episode_number": first_track.id,
                "last_season_number": album.id,
                "last_episode_number": third_track.id,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-01T00:00",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], return_url)
        self.assertTrue(ArtistTracker.objects.filter(user=self.user, artist=artist).exists())
        self.assertTrue(AlbumTracker.objects.filter(user=self.user, album=album).exists())

        plays = list(
            Music.objects.filter(user=self.user, album=album)
            .select_related("track", "item")
            .order_by("end_date", "track__track_number")
        )
        self.assertEqual(len(plays), 3)
        self.assertEqual(
            [play.track_id for play in plays],
            [first_track.id, second_track.id, third_track.id],
        )
        self.assertTrue(all(play.status == Status.COMPLETED.value for play in plays))
        self.assertLess(plays[0].end_date, plays[1].end_date)
        self.assertLess(plays[1].end_date, plays[2].end_date)
        self.assertEqual(plays[0].end_date.date(), plays[2].end_date.date())
        self.assertEqual(plays[0].item.runtime_minutes, 3)
        self.assertEqual(plays[2].item.runtime_minutes, 4)

    def test_artist_bulk_save_spans_multiple_albums_in_discography_order(self):
        artist = Artist.objects.create(name="Discography Artist")
        first_album = Album.objects.create(
            title="First Album",
            artist=artist,
            release_date=date(2024, 1, 1),
            tracks_populated=True,
        )
        second_album = Album.objects.create(
            title="Second Album",
            artist=artist,
            release_date=date(2024, 2, 1),
            tracks_populated=True,
        )
        first_track = Track.objects.create(
            album=first_album,
            title="A1",
            track_number=1,
            duration_ms=180000,
        )
        second_track = Track.objects.create(
            album=first_album,
            title="A2",
            track_number=2,
            duration_ms=210000,
        )
        third_track = Track.objects.create(
            album=second_album,
            title="B1",
            track_number=1,
            duration_ms=200000,
        )
        return_url = self._artist_detail_url(artist)

        response = self.client.post(
            f"{reverse('music_bulk_save')}?next={return_url}",
            {
                "media_id": str(artist.id),
                "source": Sources.MUSICBRAINZ.value,
                "media_type": MediaTypes.MUSIC.value,
                "library_media_type": MediaTypes.MUSIC.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": return_url,
                "context_kind": "artist",
                "context_id": str(artist.id),
                "first_season_number": first_album.id,
                "first_episode_number": first_track.id,
                "last_season_number": second_album.id,
                "last_episode_number": third_track.id,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-03-01T00:00",
                "end_date": "2024-03-03T00:00",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], return_url)
        self.assertTrue(ArtistTracker.objects.filter(user=self.user, artist=artist).exists())
        self.assertTrue(AlbumTracker.objects.filter(user=self.user, album=first_album).exists())
        self.assertTrue(AlbumTracker.objects.filter(user=self.user, album=second_album).exists())

        plays = list(
            Music.objects.filter(user=self.user, album__artist=artist)
            .select_related("track", "album")
            .order_by("end_date", "album__release_date", "track__track_number")
        )
        self.assertEqual(len(plays), 3)
        self.assertEqual(
            [(play.album_id, play.track_id) for play in plays],
            [
                (first_album.id, first_track.id),
                (first_album.id, second_track.id),
                (second_album.id, third_track.id),
            ],
        )
        self.assertLess(plays[0].end_date, plays[1].end_date)
        self.assertLess(plays[1].end_date, plays[2].end_date)

    def test_artist_bulk_save_falls_back_to_even_distribution_for_missing_release_dates(self):
        artist = Artist.objects.create(name="Fallback Artist")
        dated_album = Album.objects.create(
            title="Dated Album",
            artist=artist,
            release_date=date(2024, 1, 1),
            tracks_populated=True,
        )
        undated_album = Album.objects.create(
            title="Undated Album",
            artist=artist,
            release_date=None,
            tracks_populated=True,
        )
        first_track = Track.objects.create(
            album=dated_album,
            title="A1",
            track_number=1,
            duration_ms=180000,
        )
        second_track = Track.objects.create(
            album=undated_album,
            title="B1",
            track_number=1,
            duration_ms=210000,
        )
        third_track = Track.objects.create(
            album=undated_album,
            title="B2",
            track_number=2,
            duration_ms=220000,
        )
        return_url = self._artist_detail_url(artist)

        response = self.client.post(
            f"{reverse('music_bulk_save')}?next={return_url}",
            {
                "media_id": str(artist.id),
                "source": Sources.MUSICBRAINZ.value,
                "media_type": MediaTypes.MUSIC.value,
                "library_media_type": MediaTypes.MUSIC.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": return_url,
                "context_kind": "artist",
                "context_id": str(artist.id),
                "first_season_number": dated_album.id,
                "first_episode_number": first_track.id,
                "last_season_number": undated_album.id,
                "last_episode_number": third_track.id,
                "write_mode": "add",
                "distribution_mode": "air_date",
                "start_date": "2024-03-01T00:00",
                "end_date": "2024-03-03T00:00",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], return_url)

        plays = list(
            Music.objects.filter(user=self.user, album__artist=artist)
            .select_related("track", "album")
            .order_by("end_date", "album__release_date", "track__track_number")
        )
        self.assertEqual(
            [(play.album_id, play.track_id) for play in plays],
            [
                (dated_album.id, first_track.id),
                (undated_album.id, second_track.id),
                (undated_album.id, third_track.id),
            ],
        )
        self.assertEqual(
            [play.end_date.date().isoformat() for play in plays],
            ["2024-03-01", "2024-03-02", "2024-03-03"],
        )

    @patch("app.services.bulk_music_tracking.flush_media_change_side_effects")
    def test_music_bulk_save_flushes_side_effects_once_for_touched_days(self, mock_flush):
        artist = Artist.objects.create(name="Flush Artist")
        album = Album.objects.create(
            title="Flush Album",
            artist=artist,
            release_date=date(2024, 1, 15),
            tracks_populated=True,
        )
        first_track = Track.objects.create(
            album=album,
            title="Intro",
            track_number=1,
            duration_ms=180000,
        )
        second_track = Track.objects.create(
            album=album,
            title="Finale",
            track_number=2,
            duration_ms=210000,
        )
        first_item = Item.objects.create(
            media_id="flush-track-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title=first_track.title,
            image="https://example.com/flush-track-1.jpg",
            runtime_minutes=3,
        )
        existing_play = Music.objects.create(
            item=first_item,
            user=self.user,
            artist=artist,
            album=album,
            track=first_track,
            status=Status.COMPLETED.value,
            end_date=timezone.make_aware(datetime(2024, 1, 10, 0, 0)),
        )
        return_url = self._album_detail_url(album)

        response = self.client.post(
            f"{reverse('music_bulk_save')}?next={return_url}",
            {
                "media_id": str(album.id),
                "source": Sources.MUSICBRAINZ.value,
                "media_type": MediaTypes.MUSIC.value,
                "library_media_type": MediaTypes.MUSIC.value,
                "identity_media_type": "",
                "instance_id": "",
                "return_url": return_url,
                "context_kind": "album",
                "context_id": str(album.id),
                "first_season_number": album.id,
                "first_episode_number": first_track.id,
                "last_season_number": album.id,
                "last_episode_number": second_track.id,
                "write_mode": "add",
                "distribution_mode": "even",
                "start_date": "2024-02-01T00:00",
                "end_date": "2024-02-02T00:00",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        mock_flush.assert_called_once()
        self.assertEqual(
            mock_flush.call_args.kwargs["changed_media_type"],
            MediaTypes.MUSIC.value,
        )
        self.assertEqual(mock_flush.call_args.kwargs["reason"], "music_change")
        self.assertEqual(
            mock_flush.call_args.kwargs["history_day_keys"],
            ["20240110", "20240201", "20240202"],
        )
        self.assertEqual(
            mock_flush.call_args.kwargs["statistics_day_values"],
            ["20240110", "20240201", "20240202"],
        )

        existing_play.refresh_from_db()
        self.assertEqual(existing_play.end_date.date().isoformat(), "2024-02-01")
        self.assertTrue(
            Music.objects.filter(user=self.user, track=second_track).exists(),
        )
