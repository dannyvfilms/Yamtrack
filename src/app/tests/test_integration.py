import os
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect, sync_playwright

from app.models import Game, Item, MediaTypes, Movie, Sources, Status
from users.models import DateFormatChoices


class IntegrationTest(StaticLiveServerTestCase):
    """Integration tests for the application."""

    @classmethod
    def setUpClass(cls):
        """Set up the test class."""
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        super().setUpClass()
        cls.playwright = sync_playwright().start()
        # use headless=False, slow_mo=200 to see the browser
        cls.browser = cls.playwright.chromium.launch()
        cls.page = cls.browser.new_page()

    def setUp(self):
        """Set up test data for CustomList model."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        self.page.goto(f"{self.live_server_url}/")
        self.page.get_by_placeholder("Enter your username").fill(
            self.credentials["username"],
        )
        self.page.get_by_placeholder("Enter your password").fill(
            self.credentials["password"],
        )
        self.page.get_by_role("button", name="Sign in").click()

    @classmethod
    def tearDownClass(cls):
        """Tear down the test class."""
        super().tearDownClass()
        cls.browser.close()
        cls.playwright.stop()

    def test_season_progress_edit(self):
        """Test the progress edit of a season."""
        self.page.locator("#global-search").fill("breaking bad")
        self.page.locator("#global-search").press("Enter")
        expect(self.page.locator("h2", has_text="Search Results")).to_be_visible()
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        season_href = self.page.locator(
            'a[href*="/season/1"]',
        ).first.get_attribute("href")
        self.page.goto(f"{self.live_server_url}{season_href}")
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        self.page.get_by_title("Track Episode").first.click(force=True)
        datetime_format = "%Y-%m-%d"

        # Episode 1 air date is 2008-01-20
        fixed_date = date(2008, 1, 20)
        self.page.locator('input[name="end_date"]:visible').first.fill(
            f"{fixed_date.isoformat()}T12:00",
        )
        self.page.get_by_role("button", name="Add watch").click()

        expect(self.page.get_by_role("main")).to_contain_text(
            f"Ended: {fixed_date.strftime(datetime_format)}",
        )

        today = timezone.localtime().strftime(datetime_format)
        self.page.get_by_title("Track Episode").first.click(force=True)
        self.page.locator('input[name="end_date"]:visible').first.fill(f"{today}T12:00")
        self.page.get_by_role("button", name="Add watch").click()
        expect(self.page.get_by_role("main")).to_contain_text(f"Ended: {today}")

    def test_tv_completed(self):
        """Test the completed status of a TV show."""
        self.page.locator("#global-search").click()
        self.page.locator("#global-search").fill("breaking bad")
        self.page.locator("#global-search").press("Enter")
        expect(self.page.locator("h2", has_text="Search Results")).to_be_visible()
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        self.page.locator("button").filter(has_text="Add to tracker").click()
        expect(self.page.locator("#track-tv-1396")).to_contain_text("Score")
        self.page.get_by_label("Status").select_option("Completed")
        self.page.get_by_role("button", name="Add", exact=True).click()
        self.page.get_by_role("link", name="TV Shows").click()
        self.page.get_by_role("link", name="Table View").click()
        expect(self.page.locator("tbody")).to_contain_text("Breaking Bad")
        expect(self.page.locator("tbody")).to_contain_text("Completed")

    def test_season_completed(self):
        """Test the completed status of a season."""
        self.page.locator("#global-search").fill("breaking bad")
        self.page.locator("#global-search").press("Enter")
        expect(self.page.locator("h2", has_text="Search Results")).to_be_visible()
        self.page.get_by_title("Breaking Bad", exact=True).click()
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        season_href = self.page.locator(
            'a[href*="/season/1"]',
        ).first.get_attribute("href")
        self.page.goto(f"{self.live_server_url}{season_href}")
        expect(self.page.get_by_role("main")).to_contain_text("Breaking Bad")
        self.page.get_by_role("button", name="Add to tracker").click()
        expect(self.page.locator("#track-season-1396-1")).to_contain_text("Score")
        self.page.get_by_role("button", name="Add", exact=True).click()
        self.page.get_by_role("link", name="TV Seasons").click()
        self.page.get_by_role("link", name="Table View").click()
        expect(self.page.locator("tbody")).to_contain_text("Completed")
        expect(self.page.locator("tbody")).to_contain_text("7")

    def test_tv_manual(self):
        """Test the manual creation of a TV show."""
        # Create TV show
        self.page.get_by_role("link", name="Create Custom").click()
        self.page.get_by_placeholder("Enter title").click()
        self.page.get_by_placeholder("Enter title").fill("Friends")
        self.page.get_by_placeholder("Enter image URL").click()
        self.page.get_by_placeholder("Enter image URL").fill(
            "https://media.themoviedb.org/t/p/w300_and_h450_bestv2/2koX1xLkpTQM4IZebYvKysFW1Nh.jpg",
        )
        self.page.get_by_role("combobox").select_option("In progress")
        self.page.get_by_role("button", name="Create Entry").click()
        expect(self.page.locator(".scheme-dark")).to_contain_text(
            "Friends added successfully.",
        )

        # Create season
        self.page.get_by_role("button", name="Season").click()
        expect(self.page.get_by_role("main")).to_contain_text("Parent TV Show")
        self.page.get_by_placeholder("Search for a TV show...").click()
        self.page.get_by_placeholder("Search for a TV show...").type("fri")
        expect(self.page.locator("#parent-tv-results")).to_contain_text("Friends")
        self.page.get_by_role("button", name="Friends").click()
        self.page.get_by_placeholder("Enter image URL").click()
        self.page.get_by_placeholder("Enter image URL").fill(
            "https://media.themoviedb.org/t/p/w130_and_h195_bestv2/odCW88Cq5hAF0ZFVOkeJmeQv1nV.jpg",
        )
        self.page.get_by_role("button", name="Create Entry").click()
        expect(self.page.locator("body")).to_contain_text(
            "Friends S1 added successfully.",
        )

        # Create episode
        self.page.get_by_role("button", name="Episode").click()
        expect(self.page.get_by_role("main")).to_contain_text("Parent Season")
        self.page.get_by_placeholder("Search for a season...").click()
        self.page.get_by_placeholder("Search for a season...").type("frien")
        expect(self.page.locator("#parent-season-results")).to_contain_text(
            "Friends - Season 1",
        )
        self.page.get_by_role("button", name="Friends - Season").click()
        self.page.get_by_placeholder("Enter image URL").click()
        self.page.get_by_placeholder("Enter image URL").fill(
            "https://media.themoviedb.org/t/p/w227_and_h127_bestv2/v6Elr1W2elOyGi1MClgV0mIBVHC.jpg",
        )
        self.page.locator('input[name="end_date"]').fill("2025-03-07")
        self.page.get_by_role("button", name="Create Entry").click()
        expect(self.page.locator("body")).to_contain_text(
            "Friends S1E1 added successfully.",
        )

        # Check visibility
        self.page.get_by_role("link", name="TV Shows").click()
        self.page.get_by_role("link", name="Grid View").click()
        expect(self.page.get_by_role("main")).to_contain_text("Friends")
        self.page.get_by_role("link", name="TV Seasons").click()
        self.page.get_by_role("link", name="Grid View").click()
        expect(self.page.get_by_role("main")).to_contain_text("Season 1")
        self.page.get_by_role("link", name="TV Shows").click()
        self.page.get_by_title("Friends").click()
        expect(self.page.get_by_role("main")).to_contain_text("Friends")
        season_href = self.page.locator(
            'a[href*="/season/1"]',
        ).first.get_attribute("href")
        self.page.goto(f"{self.live_server_url}{season_href}")
        expect(self.page.get_by_role("main")).to_contain_text("Friends")
        expect(self.page.get_by_role("main")).to_contain_text("Episode 1")

    @patch("app.providers.services.get_media_metadata")
    def test_movie_split_track_modal_close_button_and_release_date(
        self,
        mock_get_metadata,
    ):
        """Shared modal close should survive split flows and release-date use."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "score": 7.6,
            "score_count": 42000,
            "details": {
                "release_date": "2019-11-08",
            },
            "related": {},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
            runtime_minutes=95,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 1, 14, 0, tzinfo=UTC),
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 14, 0, tzinfo=UTC),
        )

        self.page.goto(
            self.live_server_url
            + reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        expect(self.page.get_by_role("main")).to_contain_text("Test Movie")

        self.page.get_by_role("button", name="More tracking actions").click()
        expect(self.page.get_by_role("button", name="Add new entry")).to_be_visible()
        self.page.get_by_role("button", name="Add new entry").click()

        create_modal = self.page.locator("[data-track-modal-root]:visible").first
        expect(create_modal).to_be_visible()
        create_modal.locator("button[type='button']").first.click()
        expect(self.page.locator("[data-track-modal-root]:visible")).to_have_count(0)

        self.page.get_by_role("button", name="More tracking actions").click()
        self.page.get_by_role("button", name="Add new entry").click()
        expect(create_modal).to_be_visible()

        end_date_input = create_modal.locator('input[name="end_date"]')
        end_date_before = end_date_input.input_value()
        self.assertIn("T", end_date_before)
        end_time_segment = end_date_before.split("T", 1)[1]
        start_date_input = create_modal.locator('input[name="start_date"]')

        create_modal.get_by_role("button", name="Release date").nth(1).click()
        expect(end_date_input).to_have_value(f"2019-11-08T{end_time_segment}")
        end_hour, end_minute = [int(segment) for segment in end_time_segment.split(":")]
        expected_start_date = (
            datetime(2019, 11, 8, end_hour, end_minute, tzinfo=UTC)
            - timedelta(minutes=95)
        ).strftime("%Y-%m-%dT%H:%M")
        expect(start_date_input).to_have_value(expected_start_date)

        create_modal.locator("button[type='button']").first.click()
        expect(self.page.locator("[data-track-modal-root]:visible")).to_have_count(0)

        self.page.get_by_role("button", name="Completed", exact=True).click()
        edit_modal = self.page.locator("[data-track-modal-root]:visible").first
        expect(edit_modal).to_be_visible()
        edit_modal.locator("button[type='button']").first.click()
        expect(self.page.locator("[data-track-modal-root]:visible")).to_have_count(0)

    @patch("app.models.Item.fetch_releases")
    @patch("app.views._should_queue_game_lengths_refresh", return_value=False)
    @patch("app.providers.services.get_media_metadata")
    def test_game_progress_live_updates_start_date(
        self,
        mock_get_metadata,
        _mock_should_queue_game_lengths_refresh,
        _mock_fetch_releases,
    ):
        """Game progress should backfill the start date immediately in the modal."""
        mock_get_metadata.return_value = {
            "media_id": "186090",
            "title": "Wordle",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/wordle.jpg",
            "details": {
                "release_date": "2021-06-21",
            },
            "related": {},
            "score": 8.5,
            "score_count": 17,
        }
        item = Item.objects.create(
            media_id="186090",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Wordle",
            image="http://example.com/wordle.jpg",
            release_datetime=datetime(2021, 6, 21, 12, 0, tzinfo=UTC),
        )
        Game.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            progress=0,
            end_date=datetime(2026, 5, 12, 19, 47, tzinfo=UTC),
        )

        self.page.goto(
            self.live_server_url
            + reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "186090",
                    "title": "wordle",
                },
            ),
        )

        expect(self.page.get_by_role("main")).to_contain_text("Wordle")
        self.page.get_by_role("button", name="Planning", exact=True).click()

        modal = self.page.locator("[data-track-modal-root]:visible").first
        expect(modal).to_be_visible()

        end_date_input = modal.locator('input[name="end_date"]')
        start_date_input = modal.locator('input[name="start_date"]')
        progress_input = modal.locator('input[name="progress"]')

        end_date_value = end_date_input.input_value()
        self.assertIn("T", end_date_value)
        end_dt = datetime.strptime(end_date_value, "%Y-%m-%dT%H:%M")

        progress_input.fill("5min")

        expect(start_date_input).to_have_value(
            (end_dt - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        )
