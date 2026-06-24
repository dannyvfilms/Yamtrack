from datetime import date, datetime, timedelta
import re
from unittest.mock import call, patch

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app import history_cache, statistics as stats, statistics_cache
from app.models import (
    Album,
    Anime,
    Artist,
    Book,
    Comic,
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Episode,
    Game,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    Music,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Manga,
    Person,
    PersonGender,
    Podcast,
    Season,
    Sources,
    Status,
    Studio,
    TV,
)
from users.templatetags.user_tags import user_date_format


class StatisticsViewTests(TestCase):
    """Test the statistics view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def _create_movie_play(self, media_id, title, played_on, runtime_minutes):
        played_at = timezone.make_aware(
            datetime.combine(played_on, datetime.min.time()),
            timezone.get_current_timezone(),
        )
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image=f"http://example.com/{media_id}.jpg",
            runtime_minutes=runtime_minutes,
        )
        return Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=played_at,
            end_date=played_at,
        )

    @staticmethod
    def _credit(person, role="Lead", sort_order=0):
        return {
            "person": person,
            "role": role,
            "sort_order": sort_order,
        }

    def _mark_tmdb_credits_current(self, *items):
        for item in items:
            MetadataBackfillState.objects.update_or_create(
                item=item,
                field=MetadataBackfillField.CREDITS,
                defaults={
                    "last_success_at": timezone.now(),
                    "strategy_version": CREDITS_BACKFILL_VERSION,
                },
            )

    def _create_tmdb_tv_show(self, media_id, title, studio, show_credits, seasons, base_time):
        tv_item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image=f"http://example.com/{media_id}.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        ItemStudioCredit.objects.create(item=tv_item, studio=studio)
        for credit in show_credits:
            ItemPersonCredit.objects.create(
                item=tv_item,
                person=credit["person"],
                role_type=CreditRoleType.CAST.value,
                role=credit.get("role", "Lead"),
                sort_order=credit.get("sort_order"),
            )
        self._mark_tmdb_credits_current(tv_item)

        season_items = {}
        episode_items = {}
        for season_number, season_spec in seasons.items():
            season_item = Item.objects.create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=title,
                image=f"http://example.com/{media_id}-s{season_number}.jpg",
                season_number=season_number,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=tv,
                status=Status.COMPLETED.value,
            )
            season_items[season_number] = season_item

            for credit in season_spec.get("season_credits", []):
                ItemPersonCredit.objects.create(
                    item=season_item,
                    person=credit["person"],
                    role_type=CreditRoleType.CAST.value,
                    role=credit.get("role", "Lead"),
                    sort_order=credit.get("sort_order"),
                )
            for credit in season_spec.get("crew", []):
                ItemPersonCredit.objects.create(
                    item=season_item,
                    person=credit["person"],
                    role_type=CreditRoleType.CREW.value,
                    role=credit.get("role", "Director"),
                    department=credit.get("department", "Directing"),
                    sort_order=credit.get("sort_order"),
                )
            self._mark_tmdb_credits_current(season_item)

            for episode_spec in season_spec.get("episodes", []):
                episode_number = episode_spec["episode_number"]
                episode_item = Item.objects.create(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    title=episode_spec.get("title") or f"{title} Episode {season_number}-{episode_number}",
                    image=episode_spec.get("image") or f"http://example.com/{media_id}-s{season_number}e{episode_number}.jpg",
                    season_number=season_number,
                    episode_number=episode_number,
                    runtime_minutes=episode_spec.get("runtime_minutes", 45),
                )
                Episode.objects.create(
                    item=episode_item,
                    related_season=season,
                    end_date=base_time + timedelta(minutes=episode_spec.get("offset_minutes", 0)),
                )
                episode_items[(season_number, episode_number)] = episode_item
                for credit in episode_spec.get("credits", []):
                    ItemPersonCredit.objects.create(
                        item=episode_item,
                        person=credit["person"],
                        role_type=CreditRoleType.CAST.value,
                        role=credit.get("role", "Guest"),
                        sort_order=credit.get("sort_order"),
                    )
                for credit in episode_spec.get("crew", []):
                    ItemPersonCredit.objects.create(
                        item=episode_item,
                        person=credit["person"],
                        role_type=CreditRoleType.CREW.value,
                        role=credit.get("role", "Director"),
                        department=credit.get("department", "Directing"),
                        sort_order=credit.get("sort_order"),
                    )
                self._mark_tmdb_credits_current(episode_item)

        return {
            "tv_item": tv_item,
            "season_items": season_items,
            "episode_items": episode_items,
        }

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

    @patch("app.statistics_views.tvdb.enabled", return_value=True)
    def test_statistics_view_shows_anime_genre_preference_when_supported(self, _mock_tvdb_enabled):
        """Stats FAB should expose the TVDB anime split option only when the gate is satisfied."""
        self.user.anime_enabled = False
        self.user.save(update_fields=["anime_enabled"])

        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["tvdb_enabled"])
        self.assertContains(response, "Anime Genre (via TVDB)")

    @patch("app.statistics_views.tvdb.enabled", return_value=False)
    def test_statistics_view_hides_anime_genre_preference_without_tvdb(self, _mock_tvdb_enabled):
        """Stats FAB should hide the TVDB anime split option when TVDB is unavailable."""
        self.user.anime_enabled = False
        self.user.save(update_fields=["anime_enabled"])

        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["tvdb_enabled"])
        self.assertNotContains(response, "Anime Genre (via TVDB)")

    def test_statistics_view_uses_canonical_music_detail_links(self):
        """Music statistics cards should link through shared artist/album details routes."""
        cache.clear()
        self.client.login(**self.credentials)
        artist = Artist.objects.create(
            name="Stats Artist",
            image="http://example.com/stats-artist.jpg",
        )
        album = Album.objects.create(
            title="Stats Album",
            artist=artist,
            image="http://example.com/stats-album.jpg",
        )
        item = Item.objects.create(
            media_id="stats-track-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Stats Track",
            image="http://example.com/stats-album.jpg",
            runtime_minutes=4,
        )
        played_at = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        Music.objects.create(
            item=item,
            user=self.user,
            artist=artist,
            album=album,
            status=Status.COMPLETED.value,
            start_date=played_at,
            end_date=played_at,
        )

        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "stats-artist",
                },
            ),
        )
        self.assertContains(
            response,
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "stats-artist",
                    "album_id": album.id,
                    "album_slug": "stats-album",
                },
            ),
            count=2,
        )

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

    @patch("users.models.User.update_preference")
    def test_statistics_view_handles_preference_save_operational_error(self, mock_update_preference):
        """Statistics view should render fallback context when preference save hits sqlite lock."""
        mock_update_preference.side_effect = OperationalError("database is locked")
        today = timezone.localdate()
        year_start = today.replace(month=1, day=1)

        response = self.client.get(
            reverse("statistics")
            + f"?start-date={year_start.isoformat()}&end-date={today.isoformat()}&compare=none",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["database_error"])

    def test_statistics_view_defaults_to_previous_period_comparison(self):
        """Finite statistics ranges default to previous-period comparisons."""
        cache.clear()
        self.client.login(**self.credentials)
        current_date = date(2026, 3, 1)
        previous_date = date(2026, 2, 28)

        self._create_movie_play("movie-current-default-compare", "Current Movie", current_date, 120)
        self._create_movie_play("movie-previous-default-compare", "Previous Movie", previous_date, 60)

        response = self.client.get(
            reverse("statistics")
            + f"?start-date={current_date.isoformat()}&end-date={current_date.isoformat()}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_compare_mode"], "previous_period")

        comparison = response.context["hours_per_media_type_comparison"]["movie"]
        self.assertEqual(comparison["badge"], "Up 100%")
        self.assertEqual(comparison["badge_state"], "up")
        self.assertEqual(comparison["badge_short"], "100%")
        self.assertTrue(
            comparison["details"].endswith(response.context["comparison_range_dates_label"]),
        )
        self.assertEqual(comparison["tooltip"]["current_label"], "Current Period")
        self.assertEqual(comparison["tooltip"]["comparison_label"], "Previous Period")
        self.assertEqual(
            comparison["tooltip"]["current_total"],
            response.context["hours_per_media_type"]["movie"],
        )

    def test_statistics_view_supports_last_year_comparison(self):
        """Statistics comparison can target the same range last year."""
        cache.clear()
        self.client.login(**self.credentials)
        current_date = date(2026, 3, 1)
        last_year_date = date(2025, 3, 1)

        self._create_movie_play("movie-current-last-year", "Current Movie", current_date, 90)
        self._create_movie_play("movie-last-year", "Last Year Movie", last_year_date, 60)

        response = self.client.get(
            reverse("statistics")
            + (
                f"?start-date={current_date.isoformat()}"
                f"&end-date={current_date.isoformat()}"
                "&compare=last_year"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_compare_mode"], "last_year")

        comparison = response.context["hours_per_media_type_comparison"]["movie"]
        self.assertEqual(comparison["badge"], "Up 50%")
        self.assertEqual(comparison["badge_state"], "up")
        self.assertEqual(comparison["badge_short"], "50%")
        self.assertTrue(
            comparison["details"].endswith(response.context["comparison_range_dates_label"]),
        )
        self.assertEqual(comparison["tooltip"]["current_label"], "Current Period")
        self.assertEqual(comparison["tooltip"]["comparison_label"], "Last Year")
        self.assertEqual(
            comparison["tooltip"]["comparison_total"],
            "1h 0min",
        )

    def test_statistics_view_uses_saved_compare_mode_when_query_is_absent(self):
        """Finite statistics ranges should fall back to the saved compare preference."""
        cache.clear()
        self.user.statistics_compare_mode = "last_year"
        self.user.save(update_fields=["statistics_compare_mode"])
        self.client.login(**self.credentials)

        current_date = date(2026, 3, 1)
        last_year_date = date(2025, 3, 1)
        self._create_movie_play("movie-current-saved-last-year", "Current Movie", current_date, 90)
        self._create_movie_play("movie-saved-last-year", "Last Year Movie", last_year_date, 60)

        response = self.client.get(
            reverse("statistics")
            + f"?start-date={current_date.isoformat()}&end-date={current_date.isoformat()}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_compare_mode"], "last_year")

    def test_statistics_view_ignores_saved_compare_mode_for_all_time(self):
        """All-time statistics should still force no comparison."""
        cache.clear()
        self.user.statistics_compare_mode = "last_year"
        self.user.save(update_fields=["statistics_compare_mode"])
        self.client.login(**self.credentials)

        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_compare_mode"], "none")

    def test_statistics_view_uses_year_labels_for_ytd_last_year_comparison(self):
        """Year-to-date cards should prefer semantic year labels over raw date spans."""
        cache.clear()
        self.client.login(**self.credentials)
        today = timezone.localdate()
        year_start = today.replace(month=1, day=1)
        last_year_today = today.replace(year=today.year - 1)

        self._create_movie_play("movie-current-ytd", "Current Movie", today, 120)
        self._create_movie_play("movie-last-year-ytd", "Last Year Movie", last_year_today, 60)

        response = self.client.get(
            reverse("statistics")
            + (
                f"?start-date={year_start.isoformat()}"
                f"&end-date={today.isoformat()}"
                "&compare=last_year"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_range_name"], "This Year")
        self.assertEqual(response.context["selected_range_dates_label"], "This Year")

        comparison = response.context["hours_per_media_type_comparison"]["movie"]
        self.assertTrue(comparison["details"].endswith("last year"))
        self.assertNotIn(response.context["comparison_range_dates_label"], comparison["details"])
        self.assertEqual(comparison["tooltip"]["current_label"], "This Year")
        self.assertEqual(comparison["tooltip"]["comparison_label"], "Last Year")

    def test_statistics_view_keeps_year_label_for_stale_ytd_previous_period(self):
        """A just-stale year-to-date range should keep its semantic label."""
        cache.clear()
        self.client.login(**self.credentials)
        today = timezone.localdate()
        if today.month == 1 and today.day == 1:
            self.skipTest("No stale year-to-date window exists on the first day of the year.")

        stale_end = today - timedelta(days=1)
        year_start = today.replace(month=1, day=1)

        self._create_movie_play("movie-current-stale-ytd", "Current Movie", stale_end, 120)

        response = self.client.get(
            reverse("statistics")
            + (
                f"?start-date={year_start.isoformat()}"
                f"&end-date={stale_end.isoformat()}"
                "&compare=previous_period"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_range_name"], "This Year")
        self.assertEqual(response.context["selected_range_dates_label"], "This Year")

    def test_statistics_view_uses_month_labels_for_mtd_last_year_comparison(self):
        """Month-to-date cards should prefer semantic month labels over raw date spans."""
        cache.clear()
        self.client.login(**self.credentials)
        today = timezone.localdate()
        month_start = today.replace(day=1)
        last_year_today = today - relativedelta(years=1)

        self._create_movie_play("movie-current-mtd", "Current Movie", today, 120)
        self._create_movie_play("movie-last-year-mtd", "Last Year Movie", last_year_today, 60)

        response = self.client.get(
            reverse("statistics")
            + (
                f"?start-date={month_start.isoformat()}"
                f"&end-date={today.isoformat()}"
                "&compare=last_year"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_range_name"], "This Month")
        self.assertEqual(response.context["selected_range_dates_label"], "This Month")

        comparison = response.context["hours_per_media_type_comparison"]["movie"]
        self.assertTrue(comparison["details"].endswith("last year"))
        self.assertNotIn(response.context["comparison_range_dates_label"], comparison["details"])
        self.assertEqual(comparison["tooltip"]["current_label"], "This Month")
        self.assertEqual(comparison["tooltip"]["comparison_label"], "Last Year")

    def test_statistics_view_supports_no_comparison(self):
        """Statistics comparison can be turned off."""
        cache.clear()
        self.client.login(**self.credentials)
        current_date = date(2026, 3, 1)

        self._create_movie_play("movie-none-compare", "Current Movie", current_date, 120)

        response = self.client.get(
            reverse("statistics")
            + (
                f"?start-date={current_date.isoformat()}"
                f"&end-date={current_date.isoformat()}"
                "&compare=none"
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_compare_mode"], "none")
        comparison = response.context["hours_per_media_type_comparison"]["movie"]
        self.assertEqual(comparison["badge_state"], "none")
        self.assertEqual(comparison["badge_short"], "")
        self.assertEqual(comparison["details"], "No comparison selected")
        self.assertIsNone(comparison["tooltip"])

    def test_statistics_view_marks_first_activity_as_new(self):
        """Statistics comparison exposes a compact new-state marker for first activity."""
        cache.clear()
        self.client.login(**self.credentials)
        current_date = date(2026, 3, 1)

        self._create_movie_play("movie-new-compare", "Current Movie", current_date, 120)

        response = self.client.get(
            reverse("statistics")
            + (
                f"?start-date={current_date.isoformat()}"
                f"&end-date={current_date.isoformat()}"
                "&compare=previous_period"
            ),
        )

        self.assertEqual(response.status_code, 200)

        comparison = response.context["hours_per_media_type_comparison"]["movie"]
        self.assertEqual(comparison["badge"], "New")
        self.assertEqual(comparison["badge_state"], "new")
        self.assertEqual(comparison["badge_short"], "New")

    def test_statistics_view_uses_minutes_only_helper_for_comparison(self):
        """Comparison ranges should not trigger a second full statistics aggregation."""
        cache.clear()
        self.client.login(**self.credentials)
        current_date = date(2026, 3, 1)
        previous_date = date(2026, 2, 28)

        self._create_movie_play("movie-current-minutes-helper", "Current Movie", current_date, 120)
        self._create_movie_play("movie-previous-minutes-helper", "Previous Movie", previous_date, 60)

        with (
            patch(
                "app.views.statistics_cache.get_statistics_data",
                wraps=statistics_cache.get_statistics_data,
            ) as mock_get_statistics_data,
            patch(
                "app.views.statistics_cache.get_statistics_minutes_by_type",
                wraps=statistics_cache.get_statistics_minutes_by_type,
            ) as mock_get_statistics_minutes_by_type,
        ):
            response = self.client.get(
                reverse("statistics")
                + (
                    f"?start-date={current_date.isoformat()}"
                    f"&end-date={current_date.isoformat()}"
                    "&compare=previous_period"
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_get_statistics_data.call_count, 1)
        mock_get_statistics_minutes_by_type.assert_called_once()

    def test_statistics_view_predefined_range_cache_miss_reuses_covering_range_day_caches(self):
        """A missing predefined range cache should derive from current covering day caches."""
        cache.clear()
        self.client.login(**self.credentials)
        today = timezone.localdate()
        recent_date = today - timedelta(days=5)
        old_date = today - relativedelta(years=2)

        self._create_movie_play("movie-recent-range-cache", "Recent Movie", recent_date, 120)
        self._create_movie_play("movie-old-range-cache", "Old Movie", old_date, 90)

        statistics_cache.invalidate_statistics_cache(self.user.id)
        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        last_year_key = statistics_cache._cache_key(self.user.id, "Last 12 Months")
        last_year_lock_key = statistics_cache._refresh_lock_key(self.user.id, "Last 12 Months")
        cache.delete(last_year_key)
        cache.delete(last_year_lock_key)
        self.assertIsNone(cache.get(last_year_key))

        range_start, range_end = statistics_cache._get_predefined_range_dates("Last 12 Months")
        start_param = range_start.date().isoformat()
        end_param = range_end.date().isoformat()

        with (
            patch(
                "app.statistics_cache.refresh_statistics_cache",
                wraps=statistics_cache.refresh_statistics_cache,
            ) as mock_refresh_statistics_cache,
            patch(
                "app.statistics_cache.schedule_statistics_refresh",
                wraps=statistics_cache.schedule_statistics_refresh,
            ) as mock_schedule_statistics_refresh,
        ):
            response = self.client.get(
                reverse("statistics")
                + (
                    f"?start-date={start_param}"
                    f"&end-date={end_param}"
                    "&compare=none"
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_range_name"], "Last 12 Months")
        self.assertEqual(response.context["hours_per_media_type"]["movie"], "2h 0min")
        self.assertIsNotNone(cache.get(last_year_key))
        mock_refresh_statistics_cache.assert_not_called()
        mock_schedule_statistics_refresh.assert_not_called()

        status_response = self.client.get(
            reverse("cache_status") + "?cache_type=statistics&range_name=Last+12+Months",
        )
        self.assertEqual(status_response.status_code, 200)
        status_payload = status_response.json()
        self.assertTrue(status_payload["exists"])
        self.assertFalse(status_payload["is_refreshing"])
        self.assertFalse(status_payload["refresh_scheduled"])

    def test_statistics_view_history_highlights_use_active_boundary_days_and_deserialized_dates(self):
        """Highlight cards should use actual activity days and format cached dates correctly."""
        cache.clear()
        self.client.login(**self.credentials)
        first_play_day = date(2026, 4, 7)
        last_play_day = date(2026, 5, 5)

        self._create_movie_play("movie-highlight-first", "First Highlight Movie", first_play_day, 120)
        self._create_movie_play("movie-highlight-last", "Last Highlight Movie", last_play_day, 90)

        history_cache.cache_history_index(
            self.user.id,
            "repeats",
            history_cache.build_history_index(self.user, logging_style_override="repeats"),
        )
        history_cache._build_and_cache_history_day(
            self.user,
            history_cache._day_key_for_date(first_play_day),
            logging_style_override="repeats",
        )
        history_cache._build_and_cache_history_day(
            self.user,
            history_cache._day_key_for_date(last_play_day),
            logging_style_override="repeats",
        )

        response = self.client.get(
            reverse("statistics")
            + "?start-date=2026-04-07&end-date=2026-05-06&compare=none",
        )

        self.assertEqual(response.status_code, 200)
        highlights = response.context["history_highlights"]
        self.assertIsNotNone(highlights["first_play"])
        self.assertIsNotNone(highlights["last_play"])
        self.assertEqual(highlights["first_play"]["title"], "First Highlight Movie")
        self.assertEqual(highlights["last_play"]["title"], "Last Highlight Movie")
        self.assertIsInstance(highlights["first_play"]["played_at"], datetime)
        self.assertIsInstance(highlights["last_play"]["played_at"], datetime)

        response_body = response.content.decode()
        self.assertIn(
            f"Played {user_date_format(highlights['first_play']['played_at'], self.user)}",
            response_body,
        )
        self.assertIn(
            f"Played {user_date_format(highlights['last_play']['played_at'], self.user)}",
            response_body,
        )
        self.assertNotIn("Played 2026-04-07T", response_body)
        self.assertNotIn("Played 2026-05-05T", response_body)
        self.assertNotIn("LAST PLAY --", response_body)

    def test_statistics_view_history_highlights_fall_back_to_generic_title_when_metadata_missing(self):
        """Highlight cards should not render blank titles when source metadata is empty."""
        cache.clear()
        self.client.login(**self.credentials)
        played_at = timezone.make_aware(
            datetime.combine(date(2026, 4, 7), datetime.min.time()),
            timezone.get_current_timezone(),
        )
        item = Item.objects.create(
            media_id="podcast-empty-highlight",
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            title="",
            image="",
            runtime_minutes=45,
        )
        Podcast.objects.create(
            user=self.user,
            item=item,
            progress=45,
            start_date=played_at,
            end_date=played_at,
        )

        history_cache.cache_history_index(
            self.user.id,
            "repeats",
            history_cache.build_history_index(self.user, logging_style_override="repeats"),
        )
        history_cache._build_and_cache_history_day(
            self.user,
            history_cache._day_key_for_date(date(2026, 4, 7)),
            logging_style_override="repeats",
        )

        response = self.client.get(
            reverse("statistics")
            + "?start-date=2026-04-07&end-date=2026-04-07&compare=none",
        )

        self.assertEqual(response.status_code, 200)
        highlights = response.context["history_highlights"]
        self.assertEqual(highlights["first_play"]["title"], "Podcast")
        self.assertContains(response, "Podcast")

    def test_refresh_statistics_cache_game_daily_average_tooltip_uses_game_title(self):
        """Cached game daily-average tooltip payload should include resolved game titles."""
        cache.clear()
        now = timezone.now()
        game_item = Item.objects.create(
            media_id="tooltip-game-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Tooltip Game",
            image="http://example.com/tooltip-game.jpg",
            platforms=["PlayStation 5"],
        )
        Game.objects.create(
            user=self.user,
            item=game_item,
            status=Status.IN_PROGRESS.value,
            progress=84,
            start_date=now,
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        stats_data = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        self.assertIsNotNone(stats_data)

        by_daily_average = stats_data["game_consumption"]["charts"]["by_daily_average"]
        top_games_per_band = by_daily_average["top_games_per_band"]
        all_titles = [
            game["title"]
            for games in top_games_per_band.values()
            for game in games
        ]
        self.assertIn("Tooltip Game", all_titles)

        platform_breakdown = stats_data["game_consumption"]["platform_breakdown"]
        self.assertTrue(platform_breakdown)
        self.assertEqual(platform_breakdown[0]["name"], "PlayStation 5")

    def test_refresh_statistics_cache_handles_anime_date_ranges(self):
        """Refreshing cache should not crash for anime entries with both start and end dates."""
        cache.clear()
        now = timezone.now()
        anime_item = Item.objects.create(
            media_id="anime-range-1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Range Anime",
            image="http://example.com/range-anime.jpg",
            runtime_minutes=24,
            genres=["Action"],
        )
        Anime.objects.create(
            user=self.user,
            item=anime_item,
            status=Status.PLANNING.value,
            progress=12,
            start_date=now - timedelta(days=3),
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        stats_data = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        self.assertIsNotNone(stats_data)

    def test_statistics_view_average_rating_uses_user_rating_scale(self):
        """Average rating card should use the configured user rating scale."""
        cache.clear()
        self.client.login(**self.credentials)
        self.user.rating_scale = "5"
        self.user.save(update_fields=["rating_scale"])

        now = timezone.now()
        item = Item.objects.create(
            media_id="movie-rating-scale-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Rating Scale Movie",
            image="http://example.com/rating-scale-movie.jpg",
        )
        Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=now,
            end_date=now,
            score=8,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        score_distribution = response.context["score_distribution"]
        self.assertEqual(score_distribution["scale_max"], 5)
        self.assertEqual(score_distribution["average_score"], 4.0)
        self.assertEqual(score_distribution["labels"], [str(score) for score in range(6)])

        response_body = response.content.decode()
        self.assertRegex(
            response_body,
            re.compile(
                r"Average Rating.*?4(?:\.0+)?\s*<span[^>]*>/\s*5</span>",
                re.DOTALL,
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_passes_reading_top_genres_for_book_comic_manga(self, mock_get_metadata):
        """Book/comic/manga genre rollups should be exposed in consumption context."""
        mock_get_metadata.return_value = {"max_progress": 2000}
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        book_item = Item.objects.create(
            media_id="book-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Genre Test",
            image="http://example.com/book.jpg",
            genres=["Fantasy", "Adventure"],
        )
        comic_item = Item.objects.create(
            media_id="comic-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Comic Genre Test",
            image="http://example.com/comic.jpg",
            genres=["Sci-Fi"],
        )
        manga_item = Item.objects.create(
            media_id="manga-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga Genre Test",
            image="http://example.com/manga.jpg",
            genres=["Shonen"],
        )

        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=320,
            start_date=now - timedelta(days=3),
            end_date=now,
        )
        Comic.objects.create(
            user=self.user,
            item=comic_item,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=now - timedelta(days=2),
            end_date=now,
        )
        Manga.objects.create(
            user=self.user,
            item=manga_item,
            status=Status.IN_PROGRESS.value,
            progress=85,
            start_date=now - timedelta(days=1),
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        book_genres = [genre["name"] for genre in response.context["book_consumption"]["top_genres"]]
        comic_genres = [genre["name"] for genre in response.context["comic_consumption"]["top_genres"]]
        manga_genres = [genre["name"] for genre in response.context["manga_consumption"]["top_genres"]]

        self.assertIn("Fantasy", book_genres)
        self.assertIn("Adventure", book_genres)
        self.assertIn("Sci-Fi", comic_genres)
        self.assertIn("Shonen", manga_genres)
        response_body = response.content.decode()
        self.assertRegex(
            response_body,
            r"·\s*\d+\s+books?",
        )
        self.assertRegex(
            response_body,
            r"·\s*\d+\s+comics?",
        )
        self.assertRegex(
            response_body,
            r"·\s*\d+\s+manga\b",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_passes_book_top_authors_for_all_time(self, mock_get_metadata):
        """Book consumption should expose linked top authors for all-time statistics."""
        mock_get_metadata.return_value = {"max_progress": 500}
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        primary_author = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL1A",
            name="Primary Author",
            image="http://example.com/primary-author.jpg",
        )
        guest_author = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL2A",
            name="Guest Author",
            image="http://example.com/guest-author.jpg",
        )

        first_item = Item.objects.create(
            media_id="book-top-author-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Book One",
            image="http://example.com/book-one.jpg",
            authors=["Primary Author"],
        )
        second_item = Item.objects.create(
            media_id="book-top-author-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Book Two",
            image="http://example.com/book-two.jpg",
            authors=["Primary Author", "Guest Author"],
        )

        ItemPersonCredit.objects.create(
            item=first_item,
            person=primary_author,
            role_type=CreditRoleType.AUTHOR.value,
        )
        ItemPersonCredit.objects.create(
            item=second_item,
            person=primary_author,
            role_type=CreditRoleType.AUTHOR.value,
        )
        ItemPersonCredit.objects.create(
            item=second_item,
            person=guest_author,
            role_type=CreditRoleType.AUTHOR.value,
        )

        first_book = Book.objects.create(
            user=self.user,
            item=first_item,
            status=Status.COMPLETED.value,
            progress=300,
            start_date=now - timedelta(days=4),
            end_date=now - timedelta(days=4),
        )
        second_book = Book.objects.create(
            user=self.user,
            item=second_item,
            status=Status.COMPLETED.value,
            progress=120,
            start_date=now - timedelta(days=2),
            end_date=now - timedelta(days=2),
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_authors = response.context["book_consumption"]["top_authors"]
        self.assertEqual(top_authors[0]["name"], "Primary Author")
        self.assertEqual(top_authors[0]["units"], first_book.progress + second_book.progress)
        self.assertEqual(top_authors[0]["titles"], 2)
        self.assertEqual(top_authors[1]["name"], "Guest Author")
        self.assertEqual(top_authors[1]["units"], second_book.progress)
        self.assertEqual(top_authors[1]["titles"], 1)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL1A",
                    "name": "primary-author",
                },
            ),
        )
        self.assertContains(response, "Top 2 Authors")

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_passes_book_top_authors_for_custom_range(self, mock_get_metadata):
        """Custom-range reading statistics should aggregate top authors from day caches."""
        mock_get_metadata.return_value = {"max_progress": 500}
        cache.clear()
        self.client.login(**self.credentials)

        in_range_author = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL3A",
            name="Range Author",
        )
        out_of_range_author = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL4A",
            name="Other Author",
        )

        in_range_item = Item.objects.create(
            media_id="book-range-author-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Range Book",
            image="http://example.com/range-book.jpg",
            authors=["Range Author"],
        )
        out_of_range_item = Item.objects.create(
            media_id="book-range-author-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Out Of Range Book",
            image="http://example.com/out-of-range-book.jpg",
            authors=["Other Author"],
        )

        ItemPersonCredit.objects.create(
            item=in_range_item,
            person=in_range_author,
            role_type=CreditRoleType.AUTHOR.value,
        )
        ItemPersonCredit.objects.create(
            item=out_of_range_item,
            person=out_of_range_author,
            role_type=CreditRoleType.AUTHOR.value,
        )

        in_range_book = Book.objects.create(
            user=self.user,
            item=in_range_item,
            status=Status.COMPLETED.value,
            progress=240,
            start_date=timezone.make_aware(datetime(2026, 1, 10, 12, 0)),
            end_date=timezone.make_aware(datetime(2026, 1, 10, 12, 0)),
        )
        Book.objects.create(
            user=self.user,
            item=out_of_range_item,
            status=Status.COMPLETED.value,
            progress=180,
            start_date=timezone.make_aware(datetime(2026, 2, 10, 12, 0)),
            end_date=timezone.make_aware(datetime(2026, 2, 10, 12, 0)),
        )

        response = self.client.get(
            reverse("statistics") + "?start-date=2026-01-01&end-date=2026-01-31",
        )

        self.assertEqual(response.status_code, 200)
        top_authors = response.context["book_consumption"]["top_authors"]
        self.assertEqual(len(top_authors), 1)
        self.assertEqual(top_authors[0]["name"], "Range Author")
        self.assertEqual(top_authors[0]["units"], in_range_book.progress)
        self.assertEqual(top_authors[0]["titles"], 1)
        self.assertContains(response, "Range Author")
        self.assertNotContains(response, "Other Author")

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_passes_book_top_authors_for_this_year_cached_range(self, mock_get_metadata):
        """Predefined cached reading ranges should render top authors on the statistics page."""
        mock_get_metadata.return_value = {"max_progress": 500}
        cache.clear()
        self.client.login(**self.credentials)

        today = timezone.localdate()
        year_start = today.replace(month=1, day=1)
        played_at = timezone.make_aware(
            datetime.combine(today, datetime.min.time()),
            timezone.get_current_timezone(),
        )

        author = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL5A",
            name="Year Author",
        )
        item = Item.objects.create(
            media_id="book-year-author-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Year Book",
            image="http://example.com/year-book.jpg",
            authors=["Year Author"],
        )
        ItemPersonCredit.objects.create(
            item=item,
            person=author,
            role_type=CreditRoleType.AUTHOR.value,
        )

        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=150,
            start_date=played_at,
            end_date=played_at,
        )

        response = self.client.get(
            reverse("statistics")
            + f"?start-date={year_start.isoformat()}&end-date={today.isoformat()}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_range_name"], "This Year")
        top_authors = response.context["book_consumption"]["top_authors"]
        self.assertEqual(len(top_authors), 1)
        self.assertEqual(top_authors[0]["name"], "Year Author")
        self.assertEqual(top_authors[0]["titles"], 1)
        self.assertContains(response, "Top 1 Authors")
        self.assertContains(response, "Year Author")

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_passes_book_top_authors_from_cached_metadata(self, mock_get_metadata):
        """Book top authors should fall back to cached metadata for legacy items."""
        mock_get_metadata.return_value = {"max_progress": 500}
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        item = Item.objects.create(
            media_id="book-cache-author-1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Cached Author Book",
            image="http://example.com/cached-author-book.jpg",
            authors=[],
        )
        cache.set(
            f"{Sources.OPENLIBRARY.value}_{MediaTypes.BOOK.value}_{item.media_id}",
            {
                "authors_full": [
                    {
                        "name": "Cached Author",
                        "person_id": "OL9A",
                    },
                ],
            },
        )

        book_entry = Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=220,
            start_date=now - timedelta(days=1),
            end_date=now - timedelta(days=1),
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_authors = response.context["book_consumption"]["top_authors"]
        self.assertEqual(len(top_authors), 1)
        self.assertEqual(top_authors[0]["name"], "Cached Author")
        self.assertEqual(top_authors[0]["units"], book_entry.progress)
        self.assertEqual(top_authors[0]["titles"], 1)
        self.assertContains(response, "Cached Author")

    def test_updating_reading_scores_refreshes_top_rated_cards(self):
        """Updating reading scores should invalidate day caches used by top-rated cards."""
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        book_item = Item.objects.create(
            media_id="book-rated-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Rated Book",
            image="http://example.com/rated-book.jpg",
            genres=["Fantasy"],
        )
        comic_item = Item.objects.create(
            media_id="comic-rated-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Rated Comic",
            image="http://example.com/rated-comic.jpg",
            genres=["Sci-Fi"],
        )
        manga_item = Item.objects.create(
            media_id="manga-rated-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Rated Manga",
            image="http://example.com/rated-manga.jpg",
            genres=["Shonen"],
        )

        book_entry = Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=180,
            start_date=now - timedelta(days=125),
            end_date=now - timedelta(days=120),
            score=None,
        )
        comic_entry = Comic.objects.create(
            user=self.user,
            item=comic_item,
            status=Status.IN_PROGRESS.value,
            progress=75,
            start_date=now - timedelta(days=115),
            end_date=now - timedelta(days=110),
            score=None,
        )
        manga_entry = Manga.objects.create(
            user=self.user,
            item=manga_item,
            status=Status.IN_PROGRESS.value,
            progress=95,
            start_date=now - timedelta(days=105),
            end_date=now - timedelta(days=100),
            score=None,
        )

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        stale_response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(stale_response.context["top_rated_book"], [])
        self.assertEqual(stale_response.context["top_rated_comic"], [])
        self.assertEqual(stale_response.context["top_rated_manga"], [])

        self.assertEqual(
            self.client.post(
                reverse("update_media_score", args=[MediaTypes.BOOK.value, book_entry.id]),
                {"score": "8"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse("update_media_score", args=[MediaTypes.COMIC.value, comic_entry.id]),
                {"score": "7"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse("update_media_score", args=[MediaTypes.MANGA.value, manga_entry.id]),
                {"score": "9"},
            ).status_code,
            200,
        )

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        book_titles = [media.item.title for media in refreshed_response.context["top_rated_book"]]
        comic_titles = [media.item.title for media in refreshed_response.context["top_rated_comic"]]
        manga_titles = [media.item.title for media in refreshed_response.context["top_rated_manga"]]

        self.assertIn("Rated Book", book_titles)
        self.assertIn("Rated Comic", comic_titles)
        self.assertIn("Rated Manga", manga_titles)

    def test_refresh_statistics_cache_repairs_stale_reading_score_days(self):
        """All-time refresh should rebuild stale reading score days missed by older invalidation logic."""
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        stale_cases = [
            {
                "cache_key": MediaTypes.BOOK.value,
                "model": Book,
                "media_type": MediaTypes.BOOK.value,
                "media_id": "book-stale-score-1",
                "title": "Stale Score Book",
                "image": "http://example.com/stale-score-book.jpg",
                "genres": ["Fantasy"],
                "progress": 250,
                "offset_days": 120,
                "updated_score": 8,
            },
            {
                "cache_key": MediaTypes.COMIC.value,
                "model": Comic,
                "media_type": MediaTypes.COMIC.value,
                "media_id": "comic-stale-score-1",
                "title": "Stale Score Comic",
                "image": "http://example.com/stale-score-comic.jpg",
                "genres": ["Sci-Fi"],
                "progress": 120,
                "offset_days": 121,
                "updated_score": 9,
            },
            {
                "cache_key": MediaTypes.MANGA.value,
                "model": Manga,
                "media_type": MediaTypes.MANGA.value,
                "media_id": "manga-stale-score-1",
                "title": "Stale Score Manga",
                "image": "http://example.com/stale-score-manga.jpg",
                "genres": ["Shonen"],
                "progress": 85,
                "offset_days": 122,
                "updated_score": 10,
            },
        ]
        created_entries = []
        for case in stale_cases:
            item = Item.objects.create(
                media_id=case["media_id"],
                source=Sources.MANUAL.value,
                media_type=case["media_type"],
                title=case["title"],
                image=case["image"],
                genres=case["genres"],
            )
            entry = case["model"].objects.create(
                user=self.user,
                item=item,
                status=Status.COMPLETED.value,
                progress=case["progress"],
                start_date=None,
                end_date=now - timedelta(days=case["offset_days"]),
                score=None,
            )
            created_entries.append((case, item, entry))

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        for case, item, entry in created_entries:
            stale_day_key = history_cache.history_day_key(entry.end_date)
            stale_cache_key = statistics_cache._day_cache_key(self.user.id, stale_day_key)
            stale_day_payload = cache.get(stale_cache_key)
            stale_item_payload = stale_day_payload["items"][case["cache_key"]][str(item.id)]
            self.assertIsNone(stale_item_payload["score"])

        # Simulate legacy score updates that didn't invalidate day caches.
        for case, _item, entry in created_entries:
            case["model"].objects.filter(id=entry.id).update(score=case["updated_score"])

        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        book_titles = [media.item.title for media in refreshed_response.context["top_rated_book"]]
        comic_titles = [media.item.title for media in refreshed_response.context["top_rated_comic"]]
        manga_titles = [media.item.title for media in refreshed_response.context["top_rated_manga"]]

        self.assertIn("Stale Score Book", book_titles)
        self.assertIn("Stale Score Comic", comic_titles)
        self.assertIn("Stale Score Manga", manga_titles)

    @patch("app.providers.services.get_media_metadata")
    def test_statistics_view_returns_empty_reading_top_genres_when_items_have_no_genres(self, mock_get_metadata):
        """Reading top genres should be empty when source items have no genre metadata."""
        mock_get_metadata.return_value = {"max_progress": 2000}
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        book_item = Item.objects.create(
            media_id="book-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Without Genre",
            image="http://example.com/book-no-genre.jpg",
            genres=[],
        )
        comic_item = Item.objects.create(
            media_id="comic-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Comic Without Genre",
            image="http://example.com/comic-no-genre.jpg",
            genres=[],
        )
        manga_item = Item.objects.create(
            media_id="manga-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga Without Genre",
            image="http://example.com/manga-no-genre.jpg",
            genres=[],
        )

        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=300,
            start_date=now - timedelta(days=3),
            end_date=now,
        )
        Comic.objects.create(
            user=self.user,
            item=comic_item,
            status=Status.IN_PROGRESS.value,
            progress=110,
            start_date=now - timedelta(days=2),
            end_date=now,
        )
        Manga.objects.create(
            user=self.user,
            item=manga_item,
            status=Status.IN_PROGRESS.value,
            progress=90,
            start_date=now - timedelta(days=1),
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["book_consumption"]["top_genres"], [])
        self.assertEqual(response.context["comic_consumption"]["top_genres"], [])
        self.assertEqual(response.context["manga_consumption"]["top_genres"], [])

    @patch("app.models.providers.services.get_media_metadata")
    @patch("app.tasks.enqueue_genre_backfill_items")
    def test_build_history_day_enqueues_genre_backfill_for_reading_entries_with_missing_genres(
        self,
        mock_enqueue_genre_backfill_items,
        _mock_get_media_metadata,
    ):
        """Reading entries missing genres should enqueue genre backfill item IDs."""
        _mock_get_media_metadata.return_value = {"max_progress": 120}
        cache.clear()
        now = timezone.now()
        book_item = Item.objects.create(
            media_id="book-missing-genre",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book Missing Genre",
            image="http://example.com/book-missing-genre.jpg",
            genres=[],
        )
        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=now - timedelta(days=1),
            end_date=now,
        )

        statistics_cache.build_stats_for_day(self.user.id, now.date())

        self.assertIn(
            call([book_item.id]),
            mock_enqueue_genre_backfill_items.mock_calls,
        )

    @patch("app.statistics_aggregator._aggregate_top_talent")
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
        response_body = response.content.decode()
        top_talent_markup = response_body.split('id="top-talent-grid"', 1)[1].split("</section>", 1)[0]
        self.assertContains(response, 'id="top-talent-grid"', count=1)
        self.assertIn("3 Plays", top_talent_markup)
        self.assertNotIn("3h 20min", top_talent_markup)
        self.assertNotIn("2 Titles", top_talent_markup)

        self.user.top_talent_sort_by = "time"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertContains(response, 'id="top-talent-grid"', count=1)
        response_body = response.content.decode()
        top_talent_markup = response_body.split('id="top-talent-grid"', 1)[1].split("</section>", 1)[0]
        self.assertIn("3h 20min", top_talent_markup)
        self.assertNotIn("3 Plays", top_talent_markup)
        self.assertNotIn("2 Titles", top_talent_markup)

        self.user.top_talent_sort_by = "titles"
        self.user.save(update_fields=["top_talent_sort_by"])
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["top_talent"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertContains(response, 'id="top-talent-grid"', count=1)
        response_body = response.content.decode()
        top_talent_markup = response_body.split('id="top-talent-grid"', 1)[1].split("</section>", 1)[0]
        self.assertIn("2 Titles", top_talent_markup)
        self.assertNotIn("3 Plays", top_talent_markup)
        self.assertNotIn("3h 20min", top_talent_markup)

    def test_statistics_top_talent_precomputes_all_sort_modes(self):
        """Top talent payload should include rankings precomputed for plays, time, and titles."""
        watched_at = timezone.now()
        plays_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="301",
            name="Plays Leader",
            gender=PersonGender.MALE.value,
        )
        titles_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="302",
            name="Titles Leader",
            gender=PersonGender.MALE.value,
        )

        plays_item = Item.objects.create(
            media_id="3001",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Short Movie",
            runtime_minutes=30,
            image="http://example.com/short.jpg",
        )
        titles_item_1 = Item.objects.create(
            media_id="3002",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Long Movie One",
            runtime_minutes=60,
            image="http://example.com/long1.jpg",
        )
        titles_item_2 = Item.objects.create(
            media_id="3003",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Long Movie Two",
            runtime_minutes=60,
            image="http://example.com/long2.jpg",
        )

        ItemPersonCredit.objects.create(
            item=plays_item,
            person=plays_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=titles_item_1,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemPersonCredit.objects.create(
            item=titles_item_2,
            person=titles_actor,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )

        for offset in range(3):
            Movie.objects.create(
                item=plays_item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                start_date=watched_at + timedelta(minutes=offset),
                end_date=watched_at + timedelta(minutes=offset),
            )
        for item in (titles_item_1, titles_item_2):
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                start_date=watched_at + timedelta(minutes=10),
                end_date=watched_at + timedelta(minutes=10),
            )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_talent = response.context["top_talent"]
        self.assertIn("by_sort", top_talent)
        self.assertEqual(
            top_talent["by_sort"]["plays"]["top_actors"][0]["name"],
            "Plays Leader",
        )
        self.assertEqual(
            top_talent["by_sort"]["time"]["top_actors"][0]["name"],
            "Titles Leader",
        )
        self.assertEqual(
            top_talent["by_sort"]["titles"]["top_actors"][0]["name"],
            "Titles Leader",
        )

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_updates_preference_without_cache_rebuild(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Statistics sort autosave should persist preference without forcing cache rebuild."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        with patch(
            "app.views.statistics_cache.get_top_talent_data",
            return_value={
                "by_sort": {
                    "time": {
                        "top_actors": [
                            {
                                "source": Sources.TMDB.value,
                                "person_id": "grid-actor-1",
                                "name": "Grid Actor",
                                "image": "http://example.com/grid-actor.jpg",
                                "unique_movies": 2,
                                "unique_shows": 1,
                                "plays": 3,
                                "watched_time": "4h 0min",
                                "unique_titles": 3,
                            },
                        ],
                        "top_actresses": [],
                        "top_directors": [],
                        "top_writers": [],
                        "top_studios": [],
                    },
                },
            },
        ) as mock_get_top_talent_data:
            response = self.client.post(
                reverse("update_top_talent_sort"),
                {
                    "sort_by": "time",
                    "range_name": "All Time",
                    "start_date": "all",
                    "end_date": "all",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["sort_by"], "time")
        self.assertIn("Grid Actor", payload["grid_html"])
        self.assertIn('data-src="http://example.com/grid-actor.jpg"', payload["grid_html"])
        self.assertIn("lazyload", payload["grid_html"])
        mock_get_top_talent_data.assert_called_once_with(
            self.user,
            None,
            None,
            range_name="All Time",
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "time")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_rejects_invalid_value(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Statistics sort autosave should reject invalid values."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "invalid_sort", "range_name": "All Time"},
        )

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "plays")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    def test_update_statistics_compare_mode_updates_preference(self):
        """Statistics compare autosave should persist the selected preference."""
        self.user.statistics_compare_mode = "previous_period"
        self.user.save(update_fields=["statistics_compare_mode"])

        response = self.client.post(
            reverse("update_statistics_compare_mode"),
            {"compare_mode": "last_year"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["compare_mode"], "last_year")

        self.user.refresh_from_db()
        self.assertEqual(self.user.statistics_compare_mode, "last_year")

    def test_update_statistics_compare_mode_rejects_invalid_value(self):
        """Statistics compare autosave should reject invalid values."""
        self.user.statistics_compare_mode = "previous_period"
        self.user.save(update_fields=["statistics_compare_mode"])

        response = self.client.post(
            reverse("update_statistics_compare_mode"),
            {"compare_mode": "not_valid"},
        )

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.statistics_compare_mode, "previous_period")

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_custom_range_does_not_schedule_refresh(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Autosave with a custom range should still avoid cache rebuild side effects."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "titles", "range_name": "Custom Range"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["sort_by"], "titles")

        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "titles")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    @patch("app.views.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    def test_update_top_talent_sort_renders_requested_range_fragment(
        self,
        mock_invalidate,
        mock_refresh,
        mock_schedule_all_ranges_refresh,
    ):
        """Sort fragment requests should resolve the active statistics range without a full reload."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])

        with patch(
            "app.views.statistics_cache.get_top_talent_data",
            return_value={
                "by_sort": {
                    "titles": {
                        "top_actors": [
                            {
                                "source": Sources.TMDB.value,
                                "person_id": "range-actor-1",
                                "name": "Range Actor",
                                "image": "http://example.com/range-actor.jpg",
                                "unique_movies": 1,
                                "unique_shows": 0,
                                "plays": 1,
                                "watched_time": "2h 0min",
                                "unique_titles": 1,
                            },
                        ],
                        "top_actresses": [],
                        "top_directors": [],
                        "top_writers": [],
                        "top_studios": [],
                    },
                },
            },
        ) as mock_get_top_talent_data:
            response = self.client.post(
                reverse("update_top_talent_sort"),
                {
                    "sort_by": "titles",
                    "range_name": "Custom Range",
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["requires_reload"])
        self.assertIn("Range Actor", payload["grid_html"])
        call_args = mock_get_top_talent_data.call_args
        self.assertEqual(call_args.kwargs["range_name"], "Custom Range")
        self.assertEqual(call_args.args[0], self.user)
        self.assertEqual(call_args.args[1].date().isoformat(), "2026-01-01")
        self.assertEqual(call_args.args[2].date().isoformat(), "2026-01-31")
        self.assertEqual(call_args.args[1].time().isoformat(), "00:00:00")
        self.assertEqual(call_args.args[2].time().isoformat(), "23:59:59.999999")
        mock_invalidate.assert_not_called()
        mock_refresh.assert_not_called()
        mock_schedule_all_ranges_refresh.assert_not_called()

    @patch("app.views.statistics_cache.refresh_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    @patch("app.views.statistics_cache.range_needs_top_talent_upgrade")
    def test_update_top_talent_sort_legacy_cache_triggers_upgrade_and_reload(
        self,
        mock_range_needs_upgrade,
        mock_invalidate,
        mock_refresh,
    ):
        """Legacy cached top_talent payload should be upgraded and prompt reload."""
        self.user.top_talent_sort_by = "plays"
        self.user.save(update_fields=["top_talent_sort_by"])
        mock_range_needs_upgrade.return_value = True

        response = self.client.post(
            reverse("update_top_talent_sort"),
            {"sort_by": "time", "range_name": "All Time"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        self.assertTrue(payload["requires_reload"])
        self.assertEqual(payload["sort_by"], "time")
        mock_range_needs_upgrade.assert_called_once_with(self.user.id, "All Time")
        mock_invalidate.assert_called_once_with(self.user.id, "All Time")
        mock_refresh.assert_called_once_with(self.user.id, "All Time")

    def test_statistics_view_cached_render_avoids_n_plus_one_item_queries(self):
        """Warmed statistics cache should not fetch Item rows one-by-one during render."""
        cache.clear()
        self.client.login(**self.credentials)
        now = timezone.now()

        movie_item = Item.objects.create(
            media_id="cached-movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Cached Movie",
            image="http://example.com/cached-movie.jpg",
            runtime_minutes=120,
        )
        book_item = Item.objects.create(
            media_id="cached-book-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Cached Book",
            image="http://example.com/cached-book.jpg",
        )
        game_item = Item.objects.create(
            media_id="cached-game-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Cached Game",
            image="http://example.com/cached-game.jpg",
            platforms=["PlayStation 5"],
        )

        Movie.objects.create(
            user=self.user,
            item=movie_item,
            status=Status.COMPLETED.value,
            progress=1,
            score=9,
            start_date=now,
            end_date=now,
        )
        Book.objects.create(
            user=self.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=now,
            end_date=now,
        )
        Game.objects.create(
            user=self.user,
            item=game_item,
            status=Status.IN_PROGRESS.value,
            progress=40,
            start_date=now,
            end_date=now,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        with CaptureQueriesContext(connection) as captured_queries:
            response = self.client.get(
                reverse("statistics") + "?start-date=all&end-date=all&compare=none",
            )

        self.assertEqual(response.status_code, 200)
        item_relation_queries = [
            query["sql"]
            for query in captured_queries.captured_queries
            if 'FROM "app_item"' in query["sql"]
            and 'WHERE "app_item"."id" =' in query["sql"]
        ]
        self.assertEqual(item_relation_queries, [])

    def test_statistics_view_lazyloads_media_list_images(self):
        """Statistics list cards should lazyload their thumbnails instead of eager-loading them."""
        cache.clear()
        self.client.login(**self.credentials)
        played_at = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time()),
            timezone.get_current_timezone(),
        )

        movie_item = Item.objects.create(
            media_id="lazy-movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Lazy Movie",
            image="http://example.com/lazy-movie.jpg",
            runtime_minutes=110,
        )
        game_item = Item.objects.create(
            media_id="lazy-game-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Lazy Game",
            image="http://example.com/lazy-game.jpg",
            platforms=["PC"],
        )
        artist = Artist.objects.create(
            name="Lazy Artist",
            image="http://example.com/lazy-artist.jpg",
        )
        album = Album.objects.create(
            title="Lazy Album",
            artist=artist,
            image="http://example.com/lazy-album.jpg",
        )
        track_item = Item.objects.create(
            media_id="lazy-track-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Lazy Track",
            image="http://example.com/lazy-album.jpg",
            runtime_minutes=4,
        )

        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=played_at,
            end_date=played_at,
            score=8,
        )
        Game.objects.create(
            item=game_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=50,
            start_date=played_at,
            end_date=played_at,
        )
        Music.objects.create(
            item=track_item,
            user=self.user,
            artist=artist,
            album=album,
            status=Status.COMPLETED.value,
            start_date=played_at,
            end_date=played_at,
        )

        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        response_body = response.content.decode()
        self.assertRegex(response_body, r'<img[^>]+data-src="http://example\.com/lazy-movie\.jpg"')
        self.assertRegex(response_body, r'<img[^>]+data-src="http://example\.com/lazy-game\.jpg"')
        self.assertRegex(response_body, r'<img[^>]+data-src="http://example\.com/lazy-artist\.jpg"')
        self.assertRegex(response_body, r'<img[^>]+data-src="http://example\.com/lazy-album\.jpg"')
        self.assertNotRegex(response_body, r'<img[^>]+\ssrc="http://example\.com/lazy-movie\.jpg"')
        self.assertNotRegex(response_body, r'<img[^>]+\ssrc="http://example\.com/lazy-game\.jpg"')
        self.assertNotRegex(response_body, r'<img[^>]+\ssrc="http://example\.com/lazy-artist\.jpg"')
        self.assertNotRegex(response_body, r'<img[^>]+\ssrc="http://example\.com/lazy-album\.jpg"')

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_uses_episode_credits_with_show_fallback(self, mock_enqueue):
        """Regular TMDB show cast should still count alongside episode-specific guests."""
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
            sort_order=0,
        )
        ItemPersonCredit.objects.create(
            item=episode_item_one,
            person=episode_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        self._mark_tmdb_credits_current(show_item, season_item, episode_item_one, episode_item_two)

        mock_enqueue.reset_mock()
        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        mock_enqueue.assert_called_once()
        enqueue_args, enqueue_kwargs = mock_enqueue.call_args
        scheduled_ids = sorted(enqueue_args[0])
        self.assertIn(episode_item_two.id, scheduled_ids)
        self.assertEqual(enqueue_kwargs, {"countdown": 3})
        top_actors = response.context["top_talent"]["top_actors"]
        by_name = {entry["name"]: entry for entry in top_actors}
        self.assertIn("Episode Specific Actor", by_name)
        self.assertIn("Show Fallback Actor", by_name)
        self.assertEqual(by_name["Episode Specific Actor"]["plays"], 1)
        self.assertEqual(by_name["Show Fallback Actor"]["plays"], 2)

    @patch("app.models.providers.services.get_media_metadata", return_value={})
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_combines_episode_and_show_cast_when_both_exist(
        self,
        _mock_enqueue,
        _mock_get_media_metadata,
    ):
        """Episode plays should count both episode-level credits and show main cast credits."""
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
            sort_order=0,
        )
        ItemPersonCredit.objects.create(
            item=episode_item,
            person=episode_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        self._mark_tmdb_credits_current(show_item, season_item, episode_item)

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_talent = response.context["top_talent"]
        actress_names = {entry["name"] for entry in top_talent["top_actresses"]}
        actor_names = {entry["name"] for entry in top_talent["top_actors"]}
        self.assertIn("Show-Level Actress", actress_names)
        self.assertIn("Episode-Level Actor", actor_names)

    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_excludes_high_order_tmdb_show_guest_from_other_episodes(
        self,
        _mock_enqueue,
    ):
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="4200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Guest Count Show",
            image="http://example.com/guest-count-show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        season_item, _ = Item.objects.get_or_create(
            media_id="4200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "Guest Count Show",
                "image": "http://example.com/guest-count-season.jpg",
            },
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        first_episode_item, _ = Item.objects.get_or_create(
            media_id="4200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={
                "title": "Guest Count Episode One",
                "image": "http://example.com/guest-count-e1.jpg",
                "runtime_minutes": 42,
            },
        )
        second_episode_item, _ = Item.objects.get_or_create(
            media_id="4200",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            defaults={
                "title": "Guest Count Episode Two",
                "image": "http://example.com/guest-count-e2.jpg",
                "runtime_minutes": 44,
            },
        )
        Episode.objects.bulk_create(
            [
                Episode(
                    item=first_episode_item,
                    related_season=season,
                    end_date=watched_at,
                ),
                Episode(
                    item=second_episode_item,
                    related_season=season,
                    end_date=watched_at + timedelta(minutes=1),
                ),
            ],
        )

        guest_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="4210",
            name="High-Order Guest",
            gender=PersonGender.MALE.value,
        )
        other_actor = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="4211",
            name="Other Episode Actor",
            gender=PersonGender.MALE.value,
        )
        ItemPersonCredit.objects.create(
            item=show_item,
            person=guest_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
            sort_order=500,
        )
        ItemPersonCredit.objects.create(
            item=first_episode_item,
            person=guest_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
        )
        ItemPersonCredit.objects.create(
            item=second_episode_item,
            person=other_actor,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
        )
        self._mark_tmdb_credits_current(show_item, season_item, first_episode_item, second_episode_item)

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        by_name = {
            entry["name"]: entry
            for entry in response.context["top_talent"]["top_actors"]
        }
        self.assertEqual(by_name["High-Order Guest"]["plays"], 1)
        self.assertEqual(by_name["Other Episode Actor"]["plays"], 1)

    @patch("app.providers.services.get_media_metadata", return_value={})
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_statistics_top_talent_uses_season_episode_and_show_fallback_credit_ladder(
        self,
        _mock_enqueue,
        _mock_get_media_metadata,
    ):
        cache.clear()
        self.client.force_login(self.user)
        base_time = timezone.now().replace(second=0, microsecond=0)
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="9900",
            name="Season Ladder Studio",
        )

        stable_woman = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9901",
            name="Stable Woman",
            gender=PersonGender.FEMALE.value,
        )
        stable_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9902",
            name="Stable Helper",
            gender=PersonGender.MALE.value,
        )
        left_man = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9903",
            name="Left Man",
            gender=PersonGender.MALE.value,
        )
        left_other = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9904",
            name="Left Other",
            gender=PersonGender.MALE.value,
        )
        left_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9905",
            name="Left Helper",
            gender=PersonGender.MALE.value,
        )
        join_man = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9906",
            name="Join Man",
            gender=PersonGender.MALE.value,
        )
        join_other = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9907",
            name="Join Other",
            gender=PersonGender.MALE.value,
        )
        join_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9908",
            name="Join Helper",
            gender=PersonGender.MALE.value,
        )
        guest_woman = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9909",
            name="Guest Woman",
            gender=PersonGender.FEMALE.value,
        )
        guest_show_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9910",
            name="Guest Show Helper",
            gender=PersonGender.MALE.value,
        )
        guest_episode_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9911",
            name="Guest Episode Helper",
            gender=PersonGender.MALE.value,
        )
        season_regular_man = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9912",
            name="Season Regular Man",
            gender=PersonGender.MALE.value,
        )
        fallback_man = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9913",
            name="Fallback Man",
            gender=PersonGender.MALE.value,
        )
        mixed_episode_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9914",
            name="Mixed Episode Helper",
            gender=PersonGender.MALE.value,
        )
        mixed_season2_helper = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9915",
            name="Mixed Season 2 Helper",
            gender=PersonGender.MALE.value,
        )
        mixed_season2_director = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="9916",
            name="Mixed Season 2 Director",
            gender=PersonGender.UNKNOWN.value,
        )

        self._create_tmdb_tv_show(
            "9901",
            "Stable Ensemble",
            studio,
            show_credits=[self._credit(stable_woman, "Lead")],
            seasons={
                1: {
                    "season_credits": [self._credit(stable_woman, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 0,
                            "credits": [self._credit(stable_helper, "Support")],
                        },
                    ],
                },
                2: {
                    "season_credits": [self._credit(stable_woman, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 1,
                            "credits": [self._credit(stable_helper, "Support")],
                        },
                    ],
                },
            },
            base_time=base_time,
        )
        self._create_tmdb_tv_show(
            "9902",
            "Leaving Cast",
            studio,
            show_credits=[self._credit(left_man, "Lead")],
            seasons={
                1: {
                    "season_credits": [self._credit(left_man, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 2,
                            "credits": [self._credit(left_helper, "Support")],
                        },
                    ],
                },
                2: {
                    "season_credits": [self._credit(left_other, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 3,
                            "credits": [self._credit(left_helper, "Support")],
                        },
                    ],
                },
            },
            base_time=base_time,
        )
        self._create_tmdb_tv_show(
            "9903",
            "Joining Cast",
            studio,
            show_credits=[self._credit(join_man, "Lead")],
            seasons={
                1: {
                    "season_credits": [self._credit(join_other, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 4,
                            "credits": [self._credit(join_helper, "Support")],
                        },
                    ],
                },
                2: {
                    "season_credits": [self._credit(join_man, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 5,
                            "credits": [self._credit(join_helper, "Support")],
                        },
                    ],
                },
            },
            base_time=base_time,
        )
        self._create_tmdb_tv_show(
            "9904",
            "Guest Star Show",
            studio,
            show_credits=[self._credit(guest_show_helper, "Lead")],
            seasons={
                1: {
                    "season_credits": [],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 6,
                            "credits": [self._credit(guest_woman, "Guest")],
                        },
                        {
                            "episode_number": 2,
                            "offset_minutes": 7,
                            "credits": [self._credit(guest_episode_helper, "Guest")],
                        },
                    ],
                },
            },
            base_time=base_time,
        )
        self._create_tmdb_tv_show(
            "9905",
            "Mixed Fallback Show",
            studio,
            show_credits=[self._credit(fallback_man, "Lead")],
            seasons={
                1: {
                    "season_credits": [self._credit(season_regular_man, "Lead")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 8,
                            "credits": [self._credit(mixed_episode_helper, "Guest")],
                        },
                        {
                            "episode_number": 2,
                            "offset_minutes": 9,
                            "credits": [self._credit(mixed_episode_helper, "Guest")],
                        },
                    ],
                },
                2: {
                    "season_credits": [],
                    "crew": [self._credit(mixed_season2_director, "Director")],
                    "episodes": [
                        {
                            "episode_number": 1,
                            "offset_minutes": 10,
                            "credits": [self._credit(mixed_season2_helper, "Guest")],
                        },
                    ],
                },
            },
            base_time=base_time,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        response = self.client.get(reverse("statistics") + "?start-date=all&end-date=all")

        self.assertEqual(response.status_code, 200)
        top_talent = response.context["top_talent"]
        top_people = {
            entry["name"]: entry
            for entry in top_talent["top_actors"] + top_talent["top_actresses"]
        }
        self.assertEqual(top_people["Stable Woman"]["plays"], 2)
        self.assertEqual(top_people["Left Man"]["plays"], 1)
        self.assertEqual(top_people["Join Man"]["plays"], 1)
        self.assertEqual(top_people["Guest Woman"]["plays"], 1)
        self.assertEqual(top_people["Season Regular Man"]["plays"], 2)
        self.assertEqual(top_people["Fallback Man"]["plays"], 1)
        self.assertGreaterEqual(len(top_people), 6)

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

    @patch("app.statistics_cache.schedule_all_ranges_refresh")
    @patch("app.statistics_cache.invalidate_statistics_cache")
    @patch("app.statistics_cache.invalidate_all_statistics_days")
    def test_update_statistics_preferences_saves_tv_anime_split_and_invalidates_cache(
        self,
        mock_invalidate_all_days,
        mock_invalidate_cache,
        mock_schedule_refresh,
    ):
        """Changing the TV/anime split preference should persist and invalidate statistics cache."""
        self.assertFalse(self.user.stats_split_tv_anime)

        response = self.client.post(
            reverse("update_statistics_preferences"),
            {"stats_split_tv_anime": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.stats_split_tv_anime)
        mock_invalidate_all_days.assert_called_once_with(
            self.user.id,
            reason="statistics_preferences_changed",
        )
        mock_invalidate_cache.assert_called_once_with(self.user.id)
        mock_schedule_refresh.assert_called_once_with(
            self.user.id,
            debounce_seconds=0,
        )

    @patch("app.views.statistics_cache.schedule_statistics_refresh")
    @patch("app.views.statistics_cache.invalidate_statistics_cache")
    @patch("app.views.statistics_cache.invalidate_all_statistics_days")
    def test_refresh_statistics_clears_day_caches_before_scheduling_range_refresh(
        self,
        mock_invalidate_all_days,
        mock_invalidate_cache,
        mock_schedule_refresh,
    ):
        """Manual statistics refresh should force a full day-cache reset for the range."""
        response = self.client.post(
            reverse("refresh_statistics"),
            {"range_name": "This Year"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        mock_invalidate_all_days.assert_called_once_with(
            self.user.id,
            reason="manual_statistics_refresh:This Year",
        )
        mock_invalidate_cache.assert_called_once_with(self.user.id, "This Year")
        mock_schedule_refresh.assert_called_once_with(
            self.user.id,
            "This Year",
            debounce_seconds=0,
            countdown=0,
            allow_inline=True,
        )

    def test_get_user_media_splits_tvdb_tagged_tv_into_anime_bucket(self):
        """TV items tagged as Anime via TVDB should move from TV stats into Anime when enabled."""
        self.user.anime_enabled = False
        self.user.stats_split_tv_anime = True
        self.user.save(update_fields=["anime_enabled", "stats_split_tv_anime"])

        watched_at = timezone.now()
        tv_item = Item.objects.create(
            media_id="tvdb-anime-tv-1",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Genre Anime Show",
            image="http://example.com/genre-anime.jpg",
            genres=["Anime", "Action"],
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="tvdb-anime-tv-1-s1",
            source=Sources.TVDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Genre Anime Show Season 1",
            image="http://example.com/genre-anime-s1.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        episode_item = Item.objects.create(
            media_id="tvdb-anime-tv-1-s1e1",
            source=Sources.TVDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Genre Anime Show Episode 1",
            image="http://example.com/genre-anime-s1e1.jpg",
            season_number=1,
            episode_number=1,
            runtime_minutes=24,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=watched_at,
        )

        user_media, media_count = stats.get_user_media(self.user, None, None)

        self.assertEqual(media_count["tv"], 0)
        self.assertEqual(media_count["anime"], 1)
        self.assertFalse(user_media["tv"].exists())
        self.assertTrue(user_media["anime"].filter(pk=tv.pk).exists())

    def test_refresh_statistics_cache_splits_tvdb_tagged_tv_into_anime_bucket(self):
        """Predefined-range cache refresh should classify TVDB-tagged anime under Anime."""
        cache.clear()
        self.user.anime_enabled = False
        self.user.stats_split_tv_anime = True
        self.user.save(update_fields=["anime_enabled", "stats_split_tv_anime"])

        watched_at = timezone.now()
        tv_item = Item.objects.create(
            media_id="tvdb-anime-tv-cache-1",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Cached Genre Anime Show",
            image="http://example.com/cached-genre-anime.jpg",
            genres=["Anime", "Action"],
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="tvdb-anime-tv-cache-1-s1",
            source=Sources.TVDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Cached Genre Anime Show Season 1",
            image="http://example.com/cached-genre-anime-s1.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        episode_item = Item.objects.create(
            media_id="tvdb-anime-tv-cache-1-s1e1",
            source=Sources.TVDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Cached Genre Anime Show Episode 1",
            image="http://example.com/cached-genre-anime-s1e1.jpg",
            season_number=1,
            episode_number=1,
            runtime_minutes=24,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=watched_at,
        )

        statistics_cache.invalidate_statistics_cache(self.user.id)
        stats_data = statistics_cache.refresh_statistics_cache(self.user.id, "This Year")

        self.assertIsNotNone(stats_data)
        self.assertTrue(stats_data["anime_consumption"]["has_data"])
        self.assertFalse(stats_data["tv_consumption"]["has_data"])
        self.assertEqual(stats_data["hours_per_media_type"]["anime"], "0h 24min")
        self.assertEqual(stats_data["top_played"]["anime"][0]["media"].item.title, "Cached Genre Anime Show")
