from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Book,
    Comic,
    CreditRoleType,
    Episode,
    Game,
    Item,
    ItemPersonCredit,
    Manga,
    MediaTypes,
    Movie,
    Person,
    PersonGender,
    Season,
    Sources,
    Status,
    TV,
)


def _start_model_metadata_patches(test_case):
    """Prevent history view tests from making provider calls during model saves."""
    mock_get_media_metadata = patch(
        "app.models.providers.services.get_media_metadata",
        return_value={"max_progress": 1},
    )
    mock_fetch_releases = patch("app.models.Item.fetch_releases")
    mock_get_media_metadata.start()
    mock_fetch_releases.start()
    test_case.addCleanup(mock_get_media_metadata.stop)
    test_case.addCleanup(mock_fetch_releases.stop)


class HistoryModalViewTests(TestCase):
    """Test the history modal view."""

    def setUp(self):
        """Create a user and log in."""
        _start_model_metadata_patches(self)
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
        self.movie.end_date = timezone.now()
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

    def test_filtered_history_page_uses_full_track_modal_for_movie_cards(self):
        """Filtered history cards should open the standard track modal for editable plays."""
        response = self.client.get(
            reverse("history")
            + f"?media_type={MediaTypes.MOVIE.value}&media_id={self.item.media_id}&source={self.item.source}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'hx-get="{reverse("track_modal", kwargs={"source": Sources.TMDB.value, "media_type": MediaTypes.MOVIE.value, "media_id": "238"})}"',
            html=False,
        )
        self.assertContains(
            response,
            f'"instance_id": "{self.movie.id}"',
            html=False,
        )
        self.assertContains(response, '"standard_modal": "1"', html=False)
        self.assertContains(response, ">8<", html=False)
        self.assertNotContains(
            response,
            f'hx-get="{reverse("history_modal", kwargs={"source": Sources.TMDB.value, "media_type": MediaTypes.MOVIE.value, "media_id": "238"})}"',
            html=False,
        )

    def test_filtered_history_page_shows_game_score_inline(self):
        """Filtered game history cards should surface per-entry ratings without opening the editor."""
        game_item = Item.objects.create(
            media_id="game-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )
        Game.objects.create(
            item=game_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=120,
            score=9.5,
            start_date=timezone.now() - timedelta(hours=2),
            end_date=timezone.now(),
        )

        response = self.client.get(
            reverse("history")
            + f"?media_type={MediaTypes.GAME.value}&media_id={game_item.media_id}&source={game_item.source}&logging_style=sessions",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">9.5<", html=False)


class DeleteHistoryRecordViewTests(TestCase):
    """Test the delete history record view."""

    def setUp(self):
        """Create a user and log in."""
        _start_model_metadata_patches(self)
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
        self.movie.end_date = timezone.now()
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

    def test_delete_nonexistent_history_record(self):
        """Deleting a missing history record should return 404."""
        response = self.client.delete(
            reverse(
                "delete_history_record",
                kwargs={
                    "media_type": MediaTypes.MOVIE.value,
                    "history_id": 999999,
                },
            ),
        )

        self.assertEqual(response.status_code, 404)


class HistoryViewPersonFilterTests(TestCase):
    """Test person-based filtering on the history page."""

    def setUp(self):
        _start_model_metadata_patches(self)
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

    def test_history_person_filter_matches_episode_or_show_person_credits(self):
        tv_item = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.TMDB.value,
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
            source=Sources.TMDB.value,
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
            sort_order=0,
        )

        episode_item_match = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode Specific Match",
            image="http://example.com/tvfallback-e1.jpg",
            season_number=1,
            episode_number=1,
        )
        episode_item_exclude = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode Specific Exclusion",
            image="http://example.com/tvfallback-e2.jpg",
            season_number=1,
            episode_number=2,
        )
        episode_item_fallback = Item.objects.create(
            media_id="tv-credits-fallback",
            source=Sources.TMDB.value,
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
        self.assertIn("Episode Specific Exclusion", titles)

    def test_history_tmdb_person_filter_excludes_high_order_show_guest_from_other_episodes(self):
        target_person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="990",
            name="TMDB Guest",
            gender=PersonGender.MALE.value,
        )
        other_person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="991",
            name="Other TMDB Guest",
            gender=PersonGender.MALE.value,
        )

        tv_item = Item.objects.create(
            media_id="tv-guest-only",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="TMDB Guest Show",
            image="http://example.com/tmdbgshow.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="tv-guest-only",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="TMDB Guest Show",
            image="http://example.com/tmdbgshows1.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )

        episode_item_match = Item.objects.create(
            media_id="tv-guest-only",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="TMDB Guest Match",
            image="http://example.com/tmdbgmatch.jpg",
            season_number=1,
            episode_number=1,
        )
        episode_item_exclude = Item.objects.create(
            media_id="tv-guest-only",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="TMDB Guest Exclusion",
            image="http://example.com/tmdbgexclude.jpg",
            season_number=1,
            episode_number=2,
        )
        episode_item_fallback = Item.objects.create(
            media_id="tv-guest-only",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="TMDB Guest Fallback",
            image="http://example.com/tmdbgfallback.jpg",
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
            item=tv_item,
            person=target_person,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
            sort_order=500,
        )
        ItemPersonCredit.objects.create(
            item=episode_item_match,
            person=target_person,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
        )
        ItemPersonCredit.objects.create(
            item=episode_item_exclude,
            person=other_person,
            role_type=CreditRoleType.CAST.value,
            role="Guest Star",
        )

        response = self.client.get(
            reverse("history") + "?person_source=tmdb&person_id=990",
        )

        self.assertEqual(response.status_code, 200)
        titles = [
            entry["title"]
            for day in response.context["history_days"]
            for entry in day.get("entries", [])
        ]
        self.assertIn("TMDB Guest Match", titles)
        self.assertNotIn("TMDB Guest Exclusion", titles)
        self.assertNotIn("TMDB Guest Fallback", titles)


class HistoryViewAuthorFilterTests(TestCase):
    """Test author-based reading filters on the history page."""

    def setUp(self):
        _start_model_metadata_patches(self)
        self.credentials = {"username": "author-filter-user", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.person = Person.objects.create(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL1A",
            name="Open Author",
            gender=PersonGender.UNKNOWN.value,
        )

        now = timezone.now()

        self.book_item = Item.objects.create(
            media_id="OL123M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Credited Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=self.book_item,
            status=Status.COMPLETED.value,
            progress=350,
            start_date=now,
            end_date=now,
        )
        ItemPersonCredit.objects.create(
            item=self.book_item,
            person=self.person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )

        self.comic_item = Item.objects.create(
            media_id="comic-1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.COMIC.value,
            title="Credited Comic",
            image="http://example.com/comic.jpg",
        )
        Comic.objects.create(
            user=self.user,
            item=self.comic_item,
            status=Status.COMPLETED.value,
            progress=10,
            start_date=now,
            end_date=now,
        )
        ItemPersonCredit.objects.create(
            item=self.comic_item,
            person=self.person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Writer",
        )

        self.manga_item = Item.objects.create(
            media_id="manga-1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.MANGA.value,
            title="Credited Manga",
            image="http://example.com/manga.jpg",
        )
        Manga.objects.create(
            user=self.user,
            item=self.manga_item,
            status=Status.COMPLETED.value,
            progress=50,
            start_date=now,
            end_date=now,
        )
        ItemPersonCredit.objects.create(
            item=self.manga_item,
            person=self.person,
            role_type=CreditRoleType.AUTHOR.value,
            role="Author",
        )

        self.uncredited_book_item = Item.objects.create(
            media_id="OL999M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Uncredited Book",
            image="http://example.com/other-book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=self.uncredited_book_item,
            status=Status.COMPLETED.value,
            progress=200,
            start_date=now,
            end_date=now,
        )

    def test_history_person_filter_includes_credited_reading_entries(self):
        response = self.client.get(
            reverse("history") + "?person_source=openlibrary&person_id=OL1A",
        )

        self.assertEqual(response.status_code, 200)
        titles = [
            entry["title"]
            for day in response.context["history_days"]
            for entry in day.get("entries", [])
        ]
        self.assertIn("Credited Book", titles)
        self.assertIn("Credited Comic", titles)
        self.assertIn("Credited Manga", titles)
        self.assertNotIn("Uncredited Book", titles)

    def test_history_without_person_filter_does_not_include_reading_entries(self):
        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        titles = [
            entry["title"]
            for day in response.context["history_days"]
            for entry in day.get("entries", [])
        ]
        self.assertNotIn("Credited Book", titles)
