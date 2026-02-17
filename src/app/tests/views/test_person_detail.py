from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    CreditRoleType,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    Movie,
    Person,
    PersonGender,
    Sources,
    Status,
    Studio,
)
from users.models import DateFormatChoices


class PersonDetailViewTests(TestCase):
    """Test cast/crew person profile pages."""

    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="501",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Tracked Movie",
            image="http://example.com/tracked.jpg",
        )
        self.person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="123",
            name="Jane Star",
            gender=PersonGender.FEMALE.value,
        )
        ItemPersonCredit.objects.create(
            item=self.item,
            person=self.person,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="1",
            name="Test Studio",
        )
        ItemStudioCredit.objects.create(item=self.item, studio=studio)
        Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

    @patch("app.providers.tmdb.person")
    def test_person_detail_shows_filmography_and_history_link(self, mock_person):
        self.user.media_card_subtitle_display = "always"
        self.user.save(update_fields=["media_card_subtitle_display"])

        mock_person.return_value = {
            "person_id": "123",
            "source": Sources.TMDB.value,
            "name": "Jane Star",
            "image": "http://example.com/jane.jpg",
            "biography": "Test bio.",
            "known_for_department": "Acting",
            "gender": "female",
            "birth_date": "1990-01-01",
            "death_date": None,
            "place_of_birth": "Los Angeles",
            "filmography": [
                {
                    "media_id": "501",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "title": "Tracked Movie",
                    "image": "http://example.com/tracked.jpg",
                    "year": 2024,
                    "credit_type": "cast",
                    "role": "Lead",
                    "department": "Acting",
                },
                {
                    "media_id": "777",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "title": "Other Show",
                    "image": "http://example.com/show.jpg",
                    "year": 2021,
                    "credit_type": "cast",
                    "role": "Guest",
                    "department": "Acting",
                },
            ],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.TMDB.value,
                    "person_id": "123",
                    "name": "jane-star",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/person_detail.html")
        self.assertEqual(response.context["tracked_plays_count"], 1)
        self.assertEqual(response.context["watched_movie_count"], 1)
        self.assertEqual(response.context["watched_show_count"], 0)
        self.assertEqual(len(response.context["watched_filmography"]), 1)
        self.assertEqual(
            response.context["watched_filmography"][0]["title"],
            "Tracked Movie",
        )
        self.assertEqual(len(response.context["filmography"]), 2)
        content = response.content.decode()
        self.assertLess(content.index("Watched Content"), content.index("Filmography"))
        self.assertLess(content.index("1 movie"), content.index("1 tracked play"))
        self.assertLess(content.index("0 shows"), content.index("1 tracked play"))
        self.assertContains(response, "1 movie")
        self.assertContains(response, "0 shows")
        self.assertContains(response, "Watched")
        self.assertContains(response, "Tracked Movie")
        self.assertContains(response, "Other Show")
        self.assertContains(response, "?person_source=tmdb&amp;person_id=123")
        self.assertContains(response, "media-card-subtitle-always")
        self.assertNotContains(response, "Tracked Titles")

    @patch("app.providers.tmdb.person")
    def test_person_detail_dates_respect_user_preference(self, mock_person):
        self.user.date_format = DateFormatChoices.DD_MM_YYYY
        self.user.save(update_fields=["date_format"])

        mock_person.return_value = {
            "person_id": "123",
            "source": Sources.TMDB.value,
            "name": "Jane Star",
            "image": "http://example.com/jane.jpg",
            "biography": "",
            "known_for_department": "Acting",
            "gender": "female",
            "birth_date": "1990-01-01",
            "death_date": None,
            "place_of_birth": "Los Angeles",
            "filmography": [],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.TMDB.value,
                    "person_id": "123",
                    "name": "jane-star",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "01.01.1990")
        self.assertNotContains(response, "1990-01-01")

    def test_person_detail_rejects_non_tmdb_source(self):
        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.MAL.value,
                    "person_id": "1",
                    "name": "invalid",
                },
            ),
        )

        self.assertEqual(response.status_code, 400)
