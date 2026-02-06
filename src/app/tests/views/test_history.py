from datetime import timedelta

from django.utils import timezone
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from app.models import (
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    MediaTypes,
    Movie,
    Person,
    PersonGender,
    Season,
    Sources,
    Status,
    TV,
)


class HistoryModalViewTests(TestCase):
    """Test the history modal view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        self.movie.status = Status.COMPLETED.value
        self.movie.progress = 1
        self.movie.score = 8
        self.movie.save()

    def test_history_modal_view(self):
        """Test the history modal view."""
        response = self.client.get(
            reverse(
                "history_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            )
            + "?return_url=/home",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_history.html")

        self.assertIn("timeline", response.context)
        self.assertGreater(len(response.context["timeline"]), 0)

        first_entry = response.context["timeline"][0]
        self.assertIn("changes", first_entry)
        self.assertGreater(len(first_entry["changes"]), 0)


class DeleteHistoryRecordViewTests(TestCase):
    """Test the delete history record view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        self.movie.status = Status.COMPLETED.value
        self.movie.progress = 1
        self.movie.score = 8
        self.movie.save()

        self.history = self.movie.history.first()
        self.history_id = self.history.history_id

        # Manually update the history_user field
        self.history.history_user = self.user
        self.history.save()

    def test_delete_history_record(self):
        """Test deleting a history record."""
        # Verify the history record exists before deletion
        self.assertEqual(
            self.movie.history.filter(history_id=self.history_id).count(),
            1,
        )
        self.assertTrue(
            Movie.objects.filter(id=self.movie.id).exists(),
        )

        response = self.client.delete(
            reverse(
                "delete_history_record",
                kwargs={
                    "media_type": MediaTypes.MOVIE.value,
                    "history_id": self.history_id,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)

        # Verify the history record is actually deleted from the database
        self.assertEqual(
            self.movie.history.filter(history_id=self.history_id).count(),
            0,
        )
        # Verify the live movie instance is removed
        self.assertFalse(
            Movie.objects.filter(id=self.movie.id).exists(),
        )


class HistoryViewPersonFilterTests(TestCase):
    """Test person-based filtering on the history page."""

    def setUp(self):
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="900",
            name="Filter Person",
            gender=PersonGender.MALE.value,
        )

        self.movie_item = Item.objects.create(
            media_id="m1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Credited Movie",
            image="http://example.com/m1.jpg",
        )
        Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=self.movie_item,
            person=self.person,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )

        other_movie_item = Item.objects.create(
            media_id="m2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Other Movie",
            image="http://example.com/m2.jpg",
        )
        Movie.objects.create(
            item=other_movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        tv_item = Item.objects.create(
            media_id="tv-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Credited Show",
            image="http://example.com/tv1.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="tv-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Credited Show",
            image="http://example.com/tv1s1.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        episode_item = Item.objects.create(
            media_id="tv-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Credited Episode",
            image="http://example.com/tv1e1.jpg",
            season_number=1,
            episode_number=1,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=timezone.now(),
        )
        ItemPersonCredit.objects.create(
            item=tv_item,
            person=self.person,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )

        other_tv_item = Item.objects.create(
            media_id="tv-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Other Show",
            image="http://example.com/tv2.jpg",
        )
        other_tv = TV.objects.create(
            item=other_tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        other_season_item = Item.objects.create(
            media_id="tv-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Other Show",
            image="http://example.com/tv2s1.jpg",
            season_number=1,
        )
        other_season = Season.objects.create(
            item=other_season_item,
            user=self.user,
            related_tv=other_tv,
            status=Status.COMPLETED.value,
        )
        other_episode_item = Item.objects.create(
            media_id="tv-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Other Episode",
            image="http://example.com/tv2e1.jpg",
            season_number=1,
            episode_number=1,
        )
        Episode.objects.create(
            item=other_episode_item,
            related_season=other_season,
            end_date=timezone.now(),
        )

    def test_history_filters_by_person_source_and_id(self):
        response = self.client.get(
            reverse("history") + "?person_source=tmdb&person_id=900",
        )

        self.assertEqual(response.status_code, 200)
        titles = [
            entry["title"]
            for day in response.context["history_days"]
            for entry in day.get("entries", [])
        ]
        self.assertIn("Credited Movie", titles)
        self.assertIn("Credited Episode", titles)
        self.assertNotIn("Other Movie", titles)
        self.assertNotIn("Other Episode", titles)

    def test_history_person_filter_prefers_episode_credits_with_show_fallback(self):
        tv_item = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Credit Fallback Show",
            image="http://example.com/tvfallback.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Credit Fallback Show",
            image="http://example.com/tvfallbacks1.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )

        target_person = self.person
        other_person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="901",
            name="Other Person",
            gender=PersonGender.MALE.value,
        )

        ItemPersonCredit.objects.create(
            item=tv_item,
            person=target_person,
            role_type=CreditRoleType.CAST.value,
            role="Show-level credit",
        )

        episode_item_match = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode Specific Match",
            image="http://example.com/tvfallback-e1.jpg",
            season_number=1,
            episode_number=1,
        )
        episode_item_exclude = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode Specific Exclusion",
            image="http://example.com/tvfallback-e2.jpg",
            season_number=1,
            episode_number=2,
        )
        episode_item_fallback = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Fallback To Show Credit",
            image="http://example.com/tvfallback-e3.jpg",
            season_number=1,
            episode_number=3,
        )

        now = timezone.now()
        Episode.objects.create(
            item=episode_item_match,
            related_season=season,
            end_date=now,
        )
        Episode.objects.create(
            item=episode_item_exclude,
            related_season=season,
            end_date=now + timedelta(minutes=1),
        )
        Episode.objects.create(
            item=episode_item_fallback,
            related_season=season,
            end_date=now + timedelta(minutes=2),
        )

        ItemPersonCredit.objects.create(
            item=episode_item_match,
            person=target_person,
            role_type=CreditRoleType.CAST.value,
            role="Episode-level match",
        )
        ItemPersonCredit.objects.create(
            item=episode_item_exclude,
            person=other_person,
            role_type=CreditRoleType.CAST.value,
            role="Episode-level non-match",
        )

        response = self.client.get(
            reverse("history") + "?person_source=tmdb&person_id=900",
        )

        self.assertEqual(response.status_code, 200)
        titles = [
            entry["title"]
            for day in response.context["history_days"]
            for entry in day.get("entries", [])
        ]
        self.assertIn("Episode Specific Match", titles)
        self.assertIn("Fallback To Show Credit", titles)
        self.assertNotIn("Episode Specific Exclusion", titles)
