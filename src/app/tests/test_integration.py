import os
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone
from playwright.sync_api import expect, sync_playwright
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
        season_href = self.page.locator('a[href*="/season/1"]').first.get_attribute("href")
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
        season_href = self.page.locator('a[href*="/season/1"]').first.get_attribute("href")
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
        season_href = self.page.locator('a[href*="/season/1"]').first.get_attribute("href")
        self.page.goto(f"{self.live_server_url}{season_href}")
        expect(self.page.get_by_role("main")).to_contain_text("Friends")
        expect(self.page.get_by_role("main")).to_contain_text("Episode 1")
