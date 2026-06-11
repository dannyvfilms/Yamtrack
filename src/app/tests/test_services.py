from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.mixins import disable_fetch_releases
from app.models import (
    TV,
    Album,
    AlbumArtist,
    AlbumTracker,
    Artist,
    Book,
    Game,
    Item,
    MediaTypes,
    Movie,
    Music,
    Season,
    Sources,
    Status,
    Track,
)
from app.services.auto_pause import auto_pause_stale_items
from app.services.music import (
    canonicalize_album,
    populate_album_implied_genres,
    populate_album_tracks,
    sync_album_artist_credits,
)


class AutoPauseServiceTests(TestCase):
    """Tests for the auto-pause service."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="auto_pause_user",
            password="pass12345",
        )
        self.user.auto_pause_in_progress_enabled = True
        self.user.auto_pause_rules = [
            {"library": "all", "weeks": 20},
            {"library": MediaTypes.GAME.value, "weeks": 4},
            {"library": MediaTypes.MOVIE.value, "weeks": 20},
        ]
        self.user.save()
        self.now = timezone.now()

    def test_auto_pause_respects_specific_rules(self):
        """Specific library rules override the fallback."""
        stale_game = self._create_game("stale-game", weeks_ago=6)
        recent_game = self._create_game("fresh-game", weeks_ago=1)
        stale_movie = self._create_movie("stale-movie", weeks_ago=30)
        recent_movie = self._create_movie("fresh-movie", weeks_ago=5)
        stale_book = self._create_book("stale-book", weeks_ago=52)
        recent_book = self._create_book("fresh-book", weeks_ago=2)

        stats = auto_pause_stale_items(now=self.now)

        self.assertEqual(stats["items_paused"], 3)

        stale_game.refresh_from_db()
        self.assertEqual(stale_game.status, Status.PAUSED.value)

        recent_game.refresh_from_db()
        self.assertEqual(recent_game.status, Status.IN_PROGRESS.value)

        stale_movie.refresh_from_db()
        self.assertEqual(stale_movie.status, Status.PAUSED.value)

        recent_movie.refresh_from_db()
        self.assertEqual(recent_movie.status, Status.IN_PROGRESS.value)

        stale_book.refresh_from_db()
        self.assertEqual(stale_book.status, Status.PAUSED.value)

        recent_book.refresh_from_db()
        self.assertEqual(recent_book.status, Status.IN_PROGRESS.value)

    def test_auto_pause_handles_seasons(self):
        """Season items use their episode/end dates as activity signals."""
        self.user.auto_pause_rules = [
            {"library": MediaTypes.SEASON.value, "weeks": 12},
        ]
        self.user.save(update_fields=["auto_pause_rules"])

        tv = self._create_tv_show("tv-1")
        stale_season = self._create_season(tv, "tv-1", season_number=1, weeks_ago=40)
        fresh_season = self._create_season(tv, "tv-1", season_number=2, weeks_ago=4)

        auto_pause_stale_items(now=self.now)

        stale_season.refresh_from_db()
        fresh_season.refresh_from_db()

        self.assertEqual(stale_season.status, Status.PAUSED.value)
        self.assertEqual(fresh_season.status, Status.IN_PROGRESS.value)

    # Helpers -----------------------------------------------------------------

    def _create_item(self, media_id, media_type, **extra):
        defaults = {
            "source": Sources.MANUAL.value,
            "media_type": media_type,
            "title": f"{media_type}-{media_id}",
            "image": "https://example.com/poster.jpg",
        }
        defaults.update(extra)
        return Item.objects.create(media_id=media_id, **defaults)

    def _create_game(self, media_id, weeks_ago):
        item = self._create_item(media_id, MediaTypes.GAME.value)
        game = Game.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=60,
        )
        Game.objects.filter(id=game.id).update(status=Status.IN_PROGRESS.value)
        expected = self.now - timedelta(weeks=weeks_ago)
        game.progressed_at = expected
        game.save(update_fields=["progressed_at"])
        game.refresh_from_db()
        self.assertTrue(
            abs(game.progressed_at - expected) < timedelta(seconds=1),
            "Failed to set progressed_at on game",
        )
        return game

    def _create_movie(self, media_id, weeks_ago):
        item = self._create_item(media_id, MediaTypes.MOVIE.value)
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        Movie.objects.filter(id=movie.id).update(status=Status.IN_PROGRESS.value)
        expected = self.now - timedelta(weeks=weeks_ago)
        movie.progressed_at = expected
        movie.end_date = expected
        movie.save(update_fields=["progressed_at", "end_date"])
        movie.refresh_from_db()
        self.assertTrue(
            abs(movie.progressed_at - expected) < timedelta(seconds=1),
            "Failed to set progressed_at on movie",
        )
        return movie

    def _create_tv_show(self, media_id):
        item = self._create_item(media_id, MediaTypes.TV.value)
        return TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

    def _create_season(self, tv, media_id, season_number, weeks_ago):
        item = self._create_item(
            f"{media_id}-s{season_number}",
            MediaTypes.SEASON.value,
            season_number=season_number,
        )
        with disable_fetch_releases():
            season = Season.objects.create(
                item=item,
                user=self.user,
                related_tv=tv,
                status=Status.IN_PROGRESS.value,
            )
        Season.objects.filter(id=season.id).update(
            created_at=self.now - timedelta(weeks=weeks_ago),
        )
        return Season.objects.get(id=season.id)

    def _create_book(self, media_id, weeks_ago):
        item = self._create_item(media_id, MediaTypes.BOOK.value)
        book = Book.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=100,
        )
        Book.objects.filter(id=book.id).update(status=Status.IN_PROGRESS.value)
        expected = self.now - timedelta(weeks=weeks_ago)
        book.progressed_at = expected
        book.end_date = expected
        book.save(update_fields=["progressed_at", "end_date"])
        book.refresh_from_db()
        self.assertTrue(
            abs(book.progressed_at - expected) < timedelta(seconds=1),
            "Failed to set progressed_at on book",
        )
        return book


class MusicServiceTests(TestCase):
    """Tests for music service helpers."""

    def test_sync_album_artist_credits_replaces_fallback_credit(self):
        """Structured MusicBrainz credits replace the legacy single-artist fallback."""
        artist = Artist.objects.create(
            name="Brian Eno",
            musicbrainz_id="ff95eb47-41c4-4f7f-a104-cdc30f02e872",
        )
        album = Album.objects.create(
            title="My Life in the Bush of Ghosts",
            artist=artist,
            musicbrainz_release_id="10eaf5b7-e319-42fd-babb-d3686ad347cf",
        )
        AlbumArtist.objects.create(album=album, artist=artist)

        sync_album_artist_credits(
            album,
            {
                "artist_credits": [
                    {
                        "artist_id": "ff95eb47-41c4-4f7f-a104-cdc30f02e872",
                        "name": "Brian Eno",
                        "sort_name": "Eno, Brian",
                        "join_phrase": "\u2014",
                    },
                    {
                        "artist_id": "641b56b5-6571-43dc-b5b1-2e822f349162",
                        "name": "David Byrne",
                        "sort_name": "Byrne, David",
                        "join_phrase": "",
                    },
                ],
            },
        )

        credits = list(album.artist_credits.select_related("artist"))
        self.assertEqual(
            [credit.artist.name for credit in credits],
            ["Brian Eno", "David Byrne"],
        )
        self.assertEqual([credit.join_phrase for credit in credits], ["\u2014", ""])

    def test_canonicalize_album_merges_cross_artist_duplicate(self):
        """Shared MusicBrainz albums should use one canonical album row."""
        user = get_user_model().objects.create_user(
            username="music_user",
            password="pass12345",
        )
        brian_eno = Artist.objects.create(name="Brian Eno")
        david_byrne = Artist.objects.create(name="David Byrne")
        canonical = Album.objects.create(
            title="My Life in the Bush of Ghosts",
            artist=brian_eno,
            musicbrainz_release_group_id="release-group-mbid",
        )
        duplicate = Album.objects.create(
            title="My Life in the Bush of Ghosts",
            artist=david_byrne,
            musicbrainz_release_group_id="release-group-mbid",
        )
        AlbumArtist.objects.create(album=canonical, artist=brian_eno)
        AlbumArtist.objects.create(album=duplicate, artist=david_byrne)
        AlbumTracker.objects.create(
            user=user,
            album=canonical,
            status=Status.IN_PROGRESS.value,
        )

        result = canonicalize_album(duplicate, user=user)

        self.assertEqual(result, canonical)
        self.assertFalse(Album.objects.filter(id=duplicate.id).exists())
        self.assertCountEqual(
            list(canonical.artist_credits.values_list("artist__name", flat=True)),
            ["Brian Eno", "David Byrne"],
        )

    @patch("app.providers.musicbrainz.get_genre_parents", return_value=["Rock", "Electronic"])
    @patch("app.providers.musicbrainz.get_release_group_genres", return_value=["Krautrock"])
    @patch("app.providers.musicbrainz.get_release")
    def test_populate_album_tracks_falls_back_and_syncs_implied_genres(
        self,
        mock_get_release,
        _mock_release_group_genres,
        _mock_genre_parents,
    ):
        artist = Artist.objects.create(name="Brian Eno")
        album = Album.objects.create(
            title="My Life in the Bush of Ghosts",
            artist=artist,
            musicbrainz_release_id="release-mbid",
            musicbrainz_release_group_id="release-group-mbid",
        )
        item = Item.objects.create(
            media_id="track-mbid",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Track",
            image="https://example.com/track.jpg",
        )
        Music.objects.create(
            item=item,
            user=get_user_model().objects.create_user(
                username="music-item-user",
                password="pass12345",
            ),
            album=album,
            artist=artist,
            status=Status.COMPLETED.value,
        )
        mock_get_release.return_value = {
            "artist_credits": [],
            "genres": [],
            "tracks": [
                {
                    "disc_number": 1,
                    "track_number": 1,
                    "title": "America Is Waiting",
                    "recording_id": "track-mbid",
                    "duration_ms": 123000,
                    "genres": [],
                },
            ],
            "image": "https://example.com/album.jpg",
        }

        created_count = populate_album_tracks(album)

        self.assertEqual(created_count, 1)
        album.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(album.genres, ["Krautrock"])
        self.assertEqual(album.implied_genres, ["Rock", "Electronic"])
        self.assertEqual(item.genres, ["Krautrock"])
        self.assertEqual(item.implied_genres, ["Rock", "Electronic"])
        self.assertTrue(
            Track.objects.filter(album=album, musicbrainz_recording_id="track-mbid").exists(),
        )

    @patch("app.providers.musicbrainz.get_genre_parents", return_value=["Experimental Rock", "Rock"])
    def test_populate_album_implied_genres_excludes_direct_genres(self, _mock_genre_parents):
        artist = Artist.objects.create(name="Test Artist")
        album = Album.objects.create(
            title="Genre Album",
            artist=artist,
            genres=["Krautrock", "Rock"],
        )

        updated = populate_album_implied_genres(album)

        album.refresh_from_db()
        self.assertTrue(updated)
        self.assertEqual(album.implied_genres, ["Experimental Rock"])

    @patch("app.providers.musicbrainz.get_genre_parents", return_value=[])
    def test_populate_album_implied_genres_capitalizes_existing_direct_genres(self, _mock_genre_parents):
        artist = Artist.objects.create(name="Test Artist")
        album = Album.objects.create(
            title="Genre Album",
            artist=artist,
            genres=["art rock", "post-industrial"],
        )

        updated = populate_album_implied_genres(album)

        album.refresh_from_db()
        self.assertTrue(updated)
        self.assertEqual(album.genres, ["Art Rock", "Post-Industrial"])
