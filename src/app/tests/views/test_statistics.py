from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import statistics_cache
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


class StatisticsViewTests(TestCase):
    """Test the statistics view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_statistics_view_default_date_range(self):
        """Test the statistics view with default date range (last year)."""
        # Call the view
        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/statistics.html")

        self.assertIn("media_count", response.context)
        self.assertIn("activity_data", response.context)
        self.assertIn("media_type_distribution", response.context)
        self.assertIn("score_distribution", response.context)
        self.assertIn("status_distribution", response.context)
        self.assertIn("status_pie_chart_data", response.context)
        self.assertIn("daily_hours_by_media_type", response.context)

    def test_statistics_view_custom_date_range(self):
        """Test the statistics view with custom date range."""
        start_date = "2023-01-01"
        end_date = "2023-12-31"

        # Call the view with custom date range
        response = self.client.get(
            reverse("statistics") + f"?start-date={start_date}&end-date={end_date}",
        )

        self.assertEqual(response.status_code, 200)

        self.assertIn("media_count", response.context)
        self.assertIn("activity_data", response.context)
        self.assertIn("media_type_distribution", response.context)
        self.assertIn("score_distribution", response.context)
        self.assertIn("status_distribution", response.context)
        self.assertIn("status_pie_chart_data", response.context)
        self.assertIn("daily_hours_by_media_type", response.context)

    def test_statistics_view_invalid_date_format(self):
        """Test the statistics view with invalid date format."""
        start_date = "01/01/2023"  # MM/DD/YYYY instead of YYYY-MM-DD
        end_date = "2023/12/31"

        # Call the view with invalid date format
        response = self.client.get(
            reverse("statistics") + f"?start-date={start_date}&end-date={end_date}",
        )

        self.assertEqual(response.status_code, 200)

        date_is_none = (
            response.context["start_date"] is None
            and response.context["end_date"] is None
        )

        self.assertTrue(date_is_none)

    def test_statistics_view_includes_top_talent_sections(self):
        """Top cast/crew and studio sections should be present in context."""
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Talent Movie",
            image="http://example.com/talent.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )

        actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="100",
            name="Actor Person",
            gender=PersonGender.MALE.value,
        )
        actress = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="101",
            name="Actress Person",
            gender=PersonGender.FEMALE.value,
        )
        director = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="102",
            name="Director Person",
            gender=PersonGender.UNKNOWN.value,
        )
        writer = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="103",
            name="Writer Person",
            gender=PersonGender.UNKNOWN.value,
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="500",
            name="Studio Person",
        )

        ItemPersonCredit.objects.create(
            item=item,
            person=actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=actress,
            role_type=CreditRoleType.CAST.value,
            role="Co-Lead",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=director,
            role_type=CreditRoleType.CREW.value,
            role="Director",
            department="Directing",
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=writer,
            role_type=CreditRoleType.CREW.value,
            role="Writer",
            department="Writing",
        )
        ItemStudioCredit.objects.create(item=item, studio=studio)

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        top_talent = response.context.get("top_talent", {})
        self.assertTrue(any(entry["name"] == "Actor Person" for entry in top_talent.get("top_actors", [])))
        self.assertTrue(any(entry["name"] == "Actress Person" for entry in top_talent.get("top_actresses", [])))
        self.assertTrue(any(entry["name"] == "Director Person" for entry in top_talent.get("top_directors", [])))
        self.assertTrue(any(entry["name"] == "Writer Person" for entry in top_talent.get("top_writers", [])))
        self.assertTrue(any(entry["name"] == "Studio Person" for entry in top_talent.get("top_studios", [])))
        actor_entry = next(entry for entry in top_talent.get("top_actors", []) if entry["name"] == "Actor Person")
        studio_entry = next(entry for entry in top_talent.get("top_studios", []) if entry["name"] == "Studio Person")
        self.assertEqual(actor_entry.get("unique_movies"), 1)
        self.assertEqual(actor_entry.get("unique_shows"), 0)
        self.assertEqual(studio_entry.get("unique_movies"), 1)
        self.assertEqual(studio_entry.get("unique_shows"), 0)

    @patch("app.providers.services.get_media_metadata")
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_view_queues_credit_backfill_for_missing_tmdb_item(self, mock_enqueue, mock_get_metadata):
        """Statistics should queue credit backfill for played TMDB items missing credits."""
        mock_get_metadata.return_value = {"max_progress": 1}
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="42",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Missing Credits Movie",
            image="http://example.com/missing.jpg",
            runtime_minutes=120,
            genres=["Drama"],
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )
        mock_enqueue.reset_mock()

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_called_once_with([item.id], countdown=3)
