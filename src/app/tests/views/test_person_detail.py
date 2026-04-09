from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Book,
    Comic,
    CreditRoleType,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    Manga,
    MediaTypes,
    Movie,
    Episode,
    Person,
    PersonGender,
    Season,
    Sources,
    Status,
    Studio,
    TV,
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
            status=Status.PLANNING.value,
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

    @patch("app.models.providers.services.get_media_metadata", return_value={})
    @patch("app.providers.tmdb.person")
    def test_person_detail_counts_tv_runtime_from_show_cast_when_episode_has_other_people(
        self,
        mock_person,
        _mock_get_media_metadata,
    ):
        show_item = Item.objects.create(
            media_id="777",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Main Cast Show",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="777",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Main Cast Show",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        episode_item = Item.objects.create(
            media_id="777",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode One",
            image="http://example.com/e1.jpg",
            season_number=1,
            episode_number=1,
            runtime_minutes=45,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=timezone.now(),
        )

        ItemPersonCredit.objects.create(
            item=show_item,
            person=self.person,
            role_type=CreditRoleType.CAST.value,
            role="Main Cast",
            sort_order=0,
        )
        guest_person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="999",
            name="Guest Star",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=episode_item,
            person=guest_person,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )

        mock_person.return_value = {
            "person_id": "123",
            "source": Sources.TMDB.value,
            "name": "Jane Star",
            "image": "http://example.com/jane.jpg",
            "biography": "",
            "known_for_department": "Acting",
            "gender": "female",
            "birth_date": None,
            "death_date": None,
            "place_of_birth": "",
            "filmography": [
                {
                    "media_id": "777",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "title": "Main Cast Show",
                    "image": "http://example.com/show.jpg",
                    "year": 2022,
                    "credit_type": "cast",
                    "role": "Main Cast",
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
        self.assertEqual(response.context["tracked_plays_count"], 2)
        self.assertEqual(response.context["tracked_hours_count"], "0h 45min")

    @patch("app.models.providers.services.get_media_metadata", return_value={})
    @patch("app.providers.tmdb.person")
    def test_person_detail_does_not_count_high_order_tmdb_show_cast_on_every_episode(
        self,
        mock_person,
        _mock_get_media_metadata,
    ):
        show_item = Item.objects.create(
            media_id="778",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Guest Star Show",
            image="http://example.com/show-guest.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="778",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Guest Star Show",
            image="http://example.com/season-guest.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        first_episode_item = Item.objects.create(
            media_id="778",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Guest Episode One",
            image="http://example.com/ge1.jpg",
            season_number=1,
            episode_number=1,
            runtime_minutes=45,
        )
        second_episode_item = Item.objects.create(
            media_id="778",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Guest Episode Two",
            image="http://example.com/ge2.jpg",
            season_number=1,
            episode_number=2,
            runtime_minutes=50,
        )
        Episode.objects.create(
            item=first_episode_item,
            related_season=season,
            end_date=timezone.now(),
        )
        Episode.objects.create(
            item=second_episode_item,
            related_season=season,
            end_date=timezone.now(),
        )

        ItemPersonCredit.objects.create(
            item=show_item,
            person=self.person,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
            sort_order=500,
        )
        ItemPersonCredit.objects.create(
            item=first_episode_item,
            person=self.person,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
        )
        other_person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="998",
            name="Other Guest",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=second_episode_item,
            person=other_person,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )

        mock_person.return_value = {
            "person_id": "123",
            "source": Sources.TMDB.value,
            "name": "Jane Star",
            "image": "http://example.com/jane.jpg",
            "biography": "",
            "known_for_department": "Acting",
            "gender": "female",
            "birth_date": None,
            "death_date": None,
            "place_of_birth": "",
            "filmography": [
                {
                    "media_id": "778",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "title": "Guest Star Show",
                    "image": "http://example.com/show-guest.jpg",
                    "year": 2022,
                    "credit_type": "cast",
                    "role": "Guest Star",
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
        self.assertEqual(response.context["tracked_plays_count"], 2)
        self.assertEqual(response.context["tracked_hours_count"], "0h 45min")

    @patch("app.providers.openlibrary.author_profile")
    def test_person_detail_openlibrary_author_uses_bibliography(self, mock_author_profile):
        person = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL1A",
            name="Open Author",
        )
        item = Item.objects.create(
            media_id="OL123M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Open Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=100,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )
        mock_author_profile.return_value = {
            "person_id": "OL1A",
            "source": Sources.OPENLIBRARY.value,
            "name": "Open Author",
            "image": "http://example.com/author.jpg",
            "biography": "Open bio",
            "known_for_department": "Author",
            "birth_date": None,
            "death_date": None,
            "place_of_birth": "",
            "bibliography": [
                {
                    "media_id": "OL123M",
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "title": "Open Book",
                    "image": "http://example.com/book.jpg",
                },
            ],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL1A",
                    "name": "open-author",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_author"])
        self.assertEqual(response.context["watched_book_count"], 1)
        self.assertContains(response, "Read Content")
        self.assertContains(response, "Bibliography")
        self.assertContains(response, "1 book")
        self.assertContains(response, "1 tracked read")
        self.assertContains(response, "Open Book")
        self.assertContains(response, "?person_source=openlibrary&amp;person_id=OL1A")

    @patch("app.providers.hardcover.author_profile")
    def test_person_detail_hardcover_author_uses_bibliography(self, mock_author_profile):
        person = Person.objects.create(
            source=Sources.HARDCOVER.value,
            source_person_id="78661",
            name="George R.R. Martin",
        )
        item = Item.objects.create(
            media_id="427374",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="A Clash of Kings",
            image="http://example.com/clash.jpg",
        )
        Book.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=300,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )
        mock_author_profile.return_value = {
            "person_id": "78661",
            "source": Sources.HARDCOVER.value,
            "name": "George R.R. Martin",
            "image": "http://example.com/grrm.jpg",
            "biography": "Hardcover bio",
            "known_for_department": "Author",
            "birth_date": "1948-09-20",
            "death_date": None,
            "place_of_birth": "Bayonne",
            "bibliography": [
                {
                    "media_id": "427374",
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "title": "A Clash of Kings",
                    "image": "http://example.com/clash.jpg",
                    "year": 1998,
                },
            ],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.HARDCOVER.value,
                    "person_id": "78661",
                    "name": "george-r-r-martin",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_author"])
        self.assertEqual(response.context["watched_book_count"], 1)
        self.assertContains(response, "Read Content")
        self.assertContains(response, "Bibliography")
        self.assertContains(response, "A Clash of Kings")

    @patch("app.providers.hardcover.author_profile")
    def test_person_detail_hardcover_tracked_reads_survive_bibliography_id_mismatch(
        self,
        mock_author_profile,
    ):
        person = Person.objects.create(
            source=Sources.HARDCOVER.value,
            source_person_id="78661",
            name="George R.R. Martin",
        )
        item = Item.objects.create(
            media_id="427374",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="A Clash of Kings",
            image="http://example.com/clash.jpg",
        )
        Book.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=300,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )
        mock_author_profile.return_value = {
            "person_id": "78661",
            "source": Sources.HARDCOVER.value,
            "name": "George R.R. Martin",
            "image": "http://example.com/grrm.jpg",
            "biography": "Hardcover bio",
            "known_for_department": "Author",
            "birth_date": "1948-09-20",
            "death_date": None,
            "place_of_birth": "Bayonne",
            "bibliography": [
                {
                    "media_id": "999999",
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "title": "Mismatched Edition",
                    "image": "http://example.com/mismatch.jpg",
                    "year": 1998,
                },
            ],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.HARDCOVER.value,
                    "person_id": "78661",
                    "name": "george-r-r-martin",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tracked_plays_count"], 1)
        self.assertEqual(response.context["watched_book_count"], 1)
        self.assertEqual(len(response.context["watched_filmography"]), 1)
        self.assertEqual(
            response.context["watched_filmography"][0]["title"],
            "A Clash of Kings",
        )
        self.assertContains(response, "A Clash of Kings")

    @patch("app.providers.comicvine.person_profile")
    def test_person_detail_comicvine_falls_back_to_local_credited_items(self, mock_person_profile):
        person = Person.objects.create(
            source=Sources.COMICVINE.value,
            source_person_id="44",
            name="Comic Writer",
        )
        item = Item.objects.create(
            media_id="155969",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
            title="Ultimate Spider-Man",
            image="http://example.com/comic.jpg",
        )
        Comic.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=20,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Writer",
        )
        mock_person_profile.return_value = {
            "person_id": "44",
            "source": Sources.COMICVINE.value,
            "name": "Comic Writer",
            "image": "http://example.com/writer.jpg",
            "biography": "Writer bio",
            "known_for_department": "Writing",
            "birth_date": None,
            "death_date": None,
            "place_of_birth": "",
            "bibliography": [],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.COMICVINE.value,
                    "person_id": "44",
                    "name": "comic-writer",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_author"])
        self.assertEqual(len(response.context["filmography"]), 1)
        self.assertEqual(response.context["tracked_plays_count"], 1)
        self.assertEqual(response.context["watched_book_count"], 1)
        self.assertEqual(len(response.context["watched_filmography"]), 1)
        self.assertEqual(response.context["filmography"][0]["title"], "Ultimate Spider-Man")
        self.assertEqual(response.context["watched_filmography"][0]["title"], "Ultimate Spider-Man")
        self.assertContains(response, "Ultimate Spider-Man")

    @patch("app.providers.mangaupdates.author_profile")
    def test_person_detail_mangaupdates_tracked_reads_survive_bibliography_id_mismatch(
        self,
        mock_author_profile,
    ):
        person = Person.objects.create(
            source=Sources.MANGAUPDATES.value,
            source_person_id="991",
            name="Manga Creator",
        )
        item = Item.objects.create(
            media_id="1234",
            source=Sources.MANGAUPDATES.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga Title",
            image="http://example.com/manga.jpg",
        )
        Manga.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=40,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )
        mock_author_profile.return_value = {
            "person_id": "991",
            "source": Sources.MANGAUPDATES.value,
            "name": "Manga Creator",
            "image": "http://example.com/creator.jpg",
            "biography": "Creator bio",
            "known_for_department": "Author",
            "birth_date": None,
            "death_date": None,
            "place_of_birth": "",
            "bibliography": [
                {
                    "media_id": "9999",
                    "source": Sources.MANGAUPDATES.value,
                    "media_type": MediaTypes.MANGA.value,
                    "title": "Different Edition",
                    "image": "http://example.com/other.jpg",
                    "year": 2020,
                },
            ],
        }

        response = self.client.get(
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "person_id": "991",
                    "name": "manga-creator",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_author"])
        self.assertEqual(response.context["tracked_plays_count"], 1)
        self.assertEqual(response.context["watched_book_count"], 1)
        self.assertEqual(len(response.context["watched_filmography"]), 1)
        self.assertEqual(response.context["watched_filmography"][0]["title"], "Manga Title")
        self.assertContains(response, "Manga Title")

    def test_person_detail_rejects_unsupported_source(self):
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
