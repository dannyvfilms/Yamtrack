from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import statistics_cache
from app.models import (
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Person,
    PersonGender,
    Season,
    Sources,
    Status,
    Studio,
    TV,
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

    @patch("app.statistics_cache._aggregate_top_talent")
    def test_statistics_all_time_uses_aware_boundaries_for_top_talent(self, mock_top_talent):
        """All-time aggregation should pass aware datetime boundaries to top talent."""
        mock_top_talent.return_value = {
            "sort_by": "plays",
            "top_actors": [],
            "top_actresses": [],
            "top_directors": [],
            "top_writers": [],
            "top_studios": [],
        }

        day_list = [
            timezone.localdate() - timedelta(days=7),
            timezone.localdate(),
        ]
        statistics_cache._aggregate_statistics_from_days(
            self.user,
            day_list,
            start_date=None,
            end_date=None,
            build_missing=False,
        )

        self.assertTrue(mock_top_talent.called)
        _, start_date, end_date = mock_top_talent.call_args.args[:3]
        self.assertTrue(timezone.is_aware(start_date))
        self.assertTrue(timezone.is_aware(end_date))

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

    def test_statistics_top_talent_sort_modes_affect_rank_and_subtitle(self):
        """Top talent cards should sort and display subtitle metric by preference."""
        watched_at = timezone.now()
        titles_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="201",
            name="Titles Leader",
            gender=PersonGender.MALE.value,
        )
        plays_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="202",
            name="Plays Leader",
            gender=PersonGender.MALE.value,
        )

        titles_movie_1 = Item.objects.create(
            media_id="2001",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Titles Movie One",
            runtime_minutes=100,
            image="http://example.com/titles1.jpg",
        )
        titles_movie_2 = Item.objects.create(
            media_id="2002",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Titles Movie Two",
            runtime_minutes=100,
            image="http://example.com/titles2.jpg",
        )
        plays_movie = Item.objects.create(
            media_id="2003",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Plays Movie",
            runtime_minutes=30,
            image="http://example.com/plays.jpg",
        )

        ItemPersonCredit.objects.create(
            item=titles_movie_1,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=titles_movie_2,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=plays_movie,
            person=plays_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )

        Movie.objects.create(
            item=titles_movie_1,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at,
            end_date=watched_at,
        )
        Movie.objects.create(
            item=titles_movie_2,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=watched_at + timedelta(minutes=1),
            end_date=watched_at + timedelta(minutes=1),
        )
        for offset in range(3):
            Movie.objects.create(
                item=plays_movie,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                start_date=watched_at + timedelta(minutes=10 + offset),
                end_date=watched_at + timedelta(minutes=10 + offset),
            )

        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Plays Leader",
        )
        self.assertContains(response, "3 Plays")

        self.user.top_talent_sort_by = "time"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertContains(response, "3h 20min")

        self.user.top_talent_sort_by = "titles"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertContains(response, "2 Titles")

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_uses_episode_credits_with_show_fallback(self, mock_enqueue):
        """Episode plays should use episode credits when present, otherwise fallback to show credits."""
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Fallback Show",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        season_item, _ = Item.objects.get_or_create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "Fallback Show",
                "image": "http://example.com/season.jpg",
            },
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        episode_item_one, _ = Item.objects.get_or_create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={
                "title": "Fallback Show",
                "image": "http://example.com/e1.jpg",
                "runtime_minutes": 50,
            },
        )
        episode_item_two, _ = Item.objects.get_or_create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            defaults={
                "title": "Fallback Show",
                "image": "http://example.com/e2.jpg",
                "runtime_minutes": 50,
            },
        )

        Episode.objects.bulk_create(
            [
                Episode(
                    item=episode_item_one,
                    related_season=season,
                    end_date=watched_at,
                ),
                Episode(
                    item=episode_item_two,
                    related_season=season,
                    end_date=watched_at + timedelta(minutes=1),
                ),
            ],
        )

        show_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="301",
            name="Show Fallback Actor",
            gender=PersonGender.MALE.value,
        )
        episode_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="302",
            name="Episode Specific Actor",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=show_item,
            person=show_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=episode_item_one,
            person=episode_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        MetadataBackfillState.objects.create(
            item=episode_item_one,
            field=MetadataBackfillField.CREDITS,
            last_success_at=timezone.now(),
            strategy_version=CREDITS_BACKFILL_VERSION,
        )

        mock_enqueue.reset_mock()
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_called_once_with(
            sorted([show_item.id, episode_item_two.id]),
            countdown=3,
        )
        top_actors = response.context["top_talent"]["top_actors"]
        by_name = {entry["name"]: entry for entry in top_actors}
        self.assertIn("Episode Specific Actor", by_name)
        self.assertIn("Show Fallback Actor", by_name)
        self.assertEqual(by_name["Episode Specific Actor"]["plays"], 1)
        self.assertEqual(by_name["Show Fallback Actor"]["plays"], 1)

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_does_not_use_show_fallback_when_episode_has_people(self, _mock_enqueue):
        """Episode plays should not use show-level fallback when episode credits exist."""
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="4100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="No Category Fallback Show",
            image="http://example.com/no-fallback-show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        season_item, _ = Item.objects.get_or_create(
            media_id="4100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "No Category Fallback Show",
                "image": "http://example.com/no-fallback-season.jpg",
            },
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        episode_item, _ = Item.objects.get_or_create(
            media_id="4100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={
                "title": "No Category Fallback Show",
                "image": "http://example.com/no-fallback-e1.jpg",
                "runtime_minutes": 42,
            },
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=watched_at,
        )

        show_actress = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="4110",
            name="Show-Level Actress",
            gender=PersonGender.FEMALE.value,
        )
        episode_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="4111",
            name="Episode-Level Actor",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=show_item,
            person=show_actress,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=episode_item,
            person=episode_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        MetadataBackfillState.objects.create(
            item=episode_item,
            field=MetadataBackfillField.CREDITS,
            last_success_at=timezone.now(),
            strategy_version=CREDITS_BACKFILL_VERSION,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_talent = response.context["top_talent"]
        actress_names = {entry["name"] for entry in top_talent["top_actresses"]}
        actor_names = {entry["name"] for entry in top_talent["top_actors"]}
        self.assertNotIn("Show-Level Actress", actress_names)
        self.assertIn("Episode-Level Actor", actor_names)

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

    @patch("app.providers.services.get_media_metadata")
    @patch("app.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_refresh_statistics_schedules_credit_backfill_once_per_refresh_cycle(
        self,
        mock_enqueue,
        _mock_schedule_all_ranges_refresh,
        mock_get_metadata,
    ):
        """Day refresh should schedule missing credits without duplicate enqueue in top-talent aggregate."""
        mock_get_metadata.return_value = {"max_progress": 1}
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="9042",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Refresh Missing Credits",
            image="http://example.com/missing-refresh.jpg",
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
        mock_enqueue.return_value = 1

        statistics_cache.invalidate_statistics_cache(self.user.id)
        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        mock_enqueue.assert_called_once_with([item.id], countdown=3)

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_build_stats_for_day_backfill_payload_ignores_non_int_scheduled_count(self, mock_enqueue):
        """Cache payload should keep scheduled_credits numeric when enqueue helper is mocked."""
        watched_at = timezone.now()
        item = Item.objects.create(
            media_id="9043",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Day Missing Credits",
            image="http://example.com/missing-day.jpg",
            runtime_minutes=100,
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
        mock_enqueue.return_value = object()

        day_stats = statistics_cache.build_stats_for_day(self.user.id, watched_at.date())

        self.assertEqual(day_stats["backfill"]["missing_credits"], 1)
        self.assertEqual(day_stats["backfill"]["scheduled_credits"], 0)
