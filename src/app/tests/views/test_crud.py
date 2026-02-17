import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Anime,
    Book,
    Comic,
    Episode,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)


class CreateMedia(TestCase):
    """Test the creation of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @override_settings(MEDIA_ROOT=("create_media"))
    def test_create_anime(self):
        """Test the creation of a TV object."""
        Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "1",
                "source": Sources.MAL.value,
                "media_type": MediaTypes.ANIME.value,
                "status": Status.PLANNING.value,
                "progress": 0,
                "repeats": 0,
            },
        )
        self.assertEqual(
            Anime.objects.filter(item__media_id="1", user=self.user).exists(),
            True,
        )

    @override_settings(MEDIA_ROOT=("create_media"))
    def test_create_tv(self):
        """Test the creation of a TV object through views."""
        Item.objects.create(
            media_id="5895",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
            image="http://example.com/image.jpg",
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "5895",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )
        self.assertEqual(
            TV.objects.filter(item__media_id="5895", user=self.user).exists(),
            True,
        )

    @patch("app.views.services.get_media_metadata")
    def test_create_tv_with_null_runtime_metadata(self, metadata_mock):
        """Creating TV media should handle provider runtime=None values."""
        metadata_mock.return_value = {
            "title": "Clevatess",
            "original_title": "Clevatess",
            "localized_title": "Clevatess",
            "image": "http://example.com/image.jpg",
            "details": {
                "runtime": None,
            },
        }

        self.client.post(
            reverse("media_save"),
            {
                "media_id": "258348",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )

        self.assertEqual(
            TV.objects.filter(item__media_id="258348", user=self.user).exists(),
            True,
        )
        self.assertEqual(
            Item.objects.get(
                media_id="258348",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
            ).runtime,
            "",
        )

    @patch("app.views.services.get_media_metadata")
    def test_create_movie_sets_release_datetime_from_metadata(self, metadata_mock):
        metadata_mock.return_value = {
            "title": "The Matrix",
            "original_title": "The Matrix",
            "localized_title": "The Matrix",
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "details": {
                "release_date": "1999-03-31",
            },
        }

        self.client.post(
            reverse("media_save"),
            {
                "media_id": "603",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "status": Status.PLANNING.value,
            },
        )

        item = Item.objects.get(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        self.assertIsNotNone(item.release_datetime)
        self.assertEqual(item.release_datetime.date(), timezone.datetime(1999, 3, 31).date())

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_reading_media_persists_metadata_genres(self, view_metadata_mock, model_metadata_mock):
        metadata_by_media = {
            (MediaTypes.BOOK.value, "book-genre", Sources.OPENLIBRARY.value): {
                "title": "Book Genre",
                "original_title": "Book Genre",
                "localized_title": "Book Genre",
                "image": "http://example.com/book.jpg",
                "max_progress": 320,
                "genres": ["Fantasy", "Adventure"],
                "details": {"number_of_pages": 320},
            },
            (MediaTypes.COMIC.value, "comic-genre", Sources.COMICVINE.value): {
                "title": "Comic Genre",
                "original_title": "Comic Genre",
                "localized_title": "Comic Genre",
                "image": "http://example.com/comic.jpg",
                "max_progress": 50,
                "genres": ["Sci-Fi"],
                "details": {},
            },
            (MediaTypes.MANGA.value, "manga-genre", Sources.MANGAUPDATES.value): {
                "title": "Manga Genre",
                "original_title": "Manga Genre",
                "localized_title": "Manga Genre",
                "image": "http://example.com/manga.jpg",
                "max_progress": 120,
                "genres": ["Shonen"],
                "details": {},
            },
        }

        def _metadata_side_effect(media_type, media_id, source, *_args, **_kwargs):
            return metadata_by_media[(media_type, media_id, source)]

        view_metadata_mock.side_effect = _metadata_side_effect
        model_metadata_mock.side_effect = _metadata_side_effect

        cases = [
            (
                MediaTypes.BOOK.value,
                Sources.OPENLIBRARY.value,
                "book-genre",
                ["Fantasy", "Adventure"],
                Book,
            ),
            (
                MediaTypes.COMIC.value,
                Sources.COMICVINE.value,
                "comic-genre",
                ["Sci-Fi"],
                Comic,
            ),
            (
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                "manga-genre",
                ["Shonen"],
                Manga,
            ),
        ]

        for media_type, source, media_id, expected_genres, model in cases:
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": media_id,
                    "source": source,
                    "media_type": media_type,
                    "status": Status.PLANNING.value,
                    "progress": 0,
                },
            )

            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            self.assertEqual(item.genres, expected_genres)
            self.assertTrue(model.objects.filter(item=item, user=self.user).exists())

    def test_create_season(self):
        """Test the creation of a Season through views."""
        Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "status": Status.PLANNING.value,
            },
        )
        self.assertEqual(
            Season.objects.filter(item__media_id="1668", user=self.user).exists(),
            True,
        )

    def test_create_episodes(self):
        """Test the creation of Episode through views."""
        self.client.post(
            reverse("episode_save"),
            {
                "media_id": "1668",
                "season_number": 1,
                "episode_number": 1,
                "source": Sources.TMDB.value,
                "date": "2023-06-01T00:00",
            },
        )
        self.assertEqual(
            Episode.objects.filter(
                item__media_id="1668",
                related_season__user=self.user,
                item__episode_number=1,
            ).exists(),
            True,
        )


class EditMedia(TestCase):
    """Test the editing of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_edit_movie_score(self):
        """Test the editing of a movie score."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            score=9,
            progress=1,
            status=Status.COMPLETED.value,
            notes="Nice",
            start_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

        self.client.post(
            reverse("media_save"),
            {
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "score": 10,
                "progress": 1,
                "status": Status.COMPLETED.value,
                "notes": "Nice",
            },
        )
        self.assertEqual(Movie.objects.get(item__media_id="10494").score, 10)


class DeleteMedia(TestCase):
    """Test the deletion of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item_season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.season = Season.objects.create(
            item=self.item_season,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.item_ep = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
            episode_number=99,
        )
        self.episode = Episode.objects.create(
            item=self.item_ep,
            related_season=self.season,
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

    def test_delete_tv(self):
        """Test the deletion of a tv through views."""
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        tv_obj = TV.objects.get(user=self.user)

        self.client.post(
            reverse("media_delete"),
            data={
                "instance_id": tv_obj.id,
                "media_type": MediaTypes.TV.value,
            },
        )

        self.assertEqual(Movie.objects.filter(user=self.user).count(), 0)

    def test_delete_season(self):
        """Test the deletion of a season through views."""
        self.client.post(
            reverse(
                "media_delete",
            ),
            data={"instance_id": self.season.id, "media_type": MediaTypes.SEASON.value},
        )

        self.assertEqual(Season.objects.filter(user=self.user).count(), 0)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )

    def test_unwatch_episode(self):
        """Test unwatching of an episode through views."""
        self.client.post(
            reverse("media_delete"),
            data={
                "instance_id": self.episode.id,
                "media_type": MediaTypes.EPISODE.value,
            },
        )

        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )
