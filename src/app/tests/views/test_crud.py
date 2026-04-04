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

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_reading_media_with_null_format_metadata(self, view_metadata_mock, model_metadata_mock):
        """Creating reading media should coerce None format metadata to an empty string."""
        cases = [
            (
                MediaTypes.BOOK.value,
                Sources.HARDCOVER.value,
                "format-none-book",
                Book,
                100,
            ),
            (
                MediaTypes.COMIC.value,
                Sources.COMICVINE.value,
                "format-none-comic",
                Comic,
                10,
            ),
            (
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                "format-none-manga",
                Manga,
                10,
            ),
        ]

        for media_type, source, media_id, model_class, max_progress in cases:
            metadata = {
                "title": f"Formatless {media_type.title()}",
                "original_title": f"Formatless {media_type.title()}",
                "localized_title": f"Formatless {media_type.title()}",
                "image": "http://example.com/image.jpg",
                "max_progress": max_progress,
                "details": {
                    "format": None,
                    "number_of_pages": max_progress if media_type == MediaTypes.BOOK.value else None,
                },
            }
            view_metadata_mock.return_value = metadata
            model_metadata_mock.return_value = metadata

            response = self.client.post(
                reverse("media_save"),
                {
                    "media_id": media_id,
                    "source": source,
                    "media_type": media_type,
                    "status": Status.PLANNING.value,
                    "progress": 0,
                },
            )

            self.assertEqual(response.status_code, 302)
            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            self.assertEqual(item.format, "")
            self.assertTrue(model_class.objects.filter(item=item, user=self.user).exists())

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_comic_persists_authors_from_people_metadata(self, view_metadata_mock, model_metadata_mock):
        """Comic saves should persist provider people names into Item.authors."""
        metadata = {
            "title": "People Writer Comic",
            "original_title": "People Writer Comic",
            "localized_title": "People Writer Comic",
            "image": "http://example.com/comic.jpg",
            "details": {
                "people": ["Writer One", "Writer Two"],
            },
        }
        view_metadata_mock.return_value = metadata
        model_metadata_mock.return_value = metadata

        response = self.client.post(
            reverse("media_save"),
            {
                "media_id": "comic-people-authors",
                "source": Sources.COMICVINE.value,
                "media_type": MediaTypes.COMIC.value,
                "status": Status.PLANNING.value,
                "progress": 0,
            },
        )

        self.assertEqual(response.status_code, 302)
        item = Item.objects.get(
            media_id="comic-people-authors",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
        )
        self.assertEqual(item.authors, ["Writer One", "Writer Two"])
        self.assertTrue(Comic.objects.filter(item=item, user=self.user).exists())

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

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.views.services.get_media_metadata")
    def test_create_grouped_anime_episode_uses_provider_source(
        self,
        view_metadata_mock,
        model_metadata_mock,
    ):
        """Grouped-anime episode saves should keep provider source and anime library type."""
        tv_metadata = {
            "media_id": "76703",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Pokemon",
            "original_title": "Pokemon",
            "localized_title": "Pokemon",
            "image": "http://example.com/pokemon.jpg",
            "details": {"seasons": 2},
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "image": "http://example.com/indigo.jpg",
                    },
                    {
                        "season_number": 2,
                        "image": "http://example.com/orange.jpg",
                    },
                ],
            },
        }
        tv_with_seasons_metadata = {
            **tv_metadata,
            "season/2": {
                "media_id": "76703",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 2,
                "season_title": "Orange Islands",
                "image": "http://example.com/orange.jpg",
                "details": {"episodes": 2},
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "1999-01-28",
                        "image": "http://example.com/orange-1.jpg",
                        "name": "Pallet Party Panic",
                    },
                    {
                        "episode_number": 2,
                        "air_date": "1999-02-04",
                        "image": "http://example.com/orange-2.jpg",
                        "name": "A Scare in the Air",
                    },
                ],
            },
        }

        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            **_kwargs,
        ):
            self.assertEqual(media_id, "76703")
            self.assertEqual(source, Sources.TVDB.value)
            if media_type == "tv_with_seasons":
                self.assertEqual(season_numbers, [2])
                return tv_with_seasons_metadata
            if media_type == MediaTypes.SEASON.value:
                self.assertEqual(season_numbers, [2])
                return tv_with_seasons_metadata["season/2"]
            if media_type == MediaTypes.TV.value:
                return tv_metadata
            error_message = f"Unexpected metadata request: {media_type}"
            raise AssertionError(error_message)

        view_metadata_mock.side_effect = metadata_side_effect
        model_metadata_mock.side_effect = metadata_side_effect

        response = self.client.post(
            reverse("episode_save"),
            {
                "media_id": "76703",
                "season_number": 2,
                "episode_number": 1,
                "source": Sources.TVDB.value,
                "library_media_type": MediaTypes.ANIME.value,
                "end_date": "2024-01-01",
            },
        )

        self.assertEqual(response.status_code, 302)
        created_episode = Episode.objects.get(
            item__media_id="76703",
            item__source=Sources.TVDB.value,
            item__episode_number=1,
            related_season__user=self.user,
        )
        self.assertEqual(created_episode.related_season.item.source, Sources.TVDB.value)
        self.assertEqual(
            created_episode.related_season.item.library_media_type,
            MediaTypes.ANIME.value,
        )
        self.assertEqual(
            created_episode.related_season.related_tv.item.library_media_type,
            MediaTypes.ANIME.value,
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

    def test_edit_movie_image_url(self):
        """Test overriding a movie image URL from the edit form."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/original.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.client.post(
            reverse("media_save"),
            {
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "status": Status.PLANNING.value,
                "image_url": "https://images.example.com/custom-poster.jpg",
            },
        )

        item.refresh_from_db()
        self.assertEqual(item.image, "https://images.example.com/custom-poster.jpg")


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
