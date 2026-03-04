from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from unittest.mock import patch

from app.discover.profile import compute_taste_profile
from app.models import Item, ItemTag, MediaTypes, Movie, Sources, Status, TV, Tag


class DiscoverProfileTests(TestCase):
    """Tests for taste profile computation."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="profile-user", password="testpass")

    def test_compute_taste_profile_prefers_recent_high_weight_genres(self):
        recent_item = Item.objects.create(
            media_id="201",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Recent Action",
            image="http://example.com/recent.jpg",
            genres=["Action"],
        )
        old_item = Item.objects.create(
            media_id="202",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Old Romance",
            image="http://example.com/old.jpg",
            genres=["Romance"],
        )

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=recent_item,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=2),
            )
            Movie.objects.create(
                item=old_item,
                user=self.user,
                score=5,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=300),
            )

        tag = Tag.objects.create(user=self.user, name="Heist")
        ItemTag.objects.create(tag=tag, item=recent_item)

        profile = compute_taste_profile(self.user, MediaTypes.MOVIE.value)

        self.assertIn("action", profile.genre_affinity)
        self.assertGreater(profile.genre_affinity["action"], profile.genre_affinity.get("romance", 0))
        self.assertIn("heist", profile.tag_affinity)

    def test_compute_taste_profile_tv_works_without_end_date_field(self):
        tv_item = Item.objects.create(
            media_id="501",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="A TV Show",
            image="http://example.com/tv.jpg",
            genres=["Drama"],
        )
        TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            score=8,
        )

        profile = compute_taste_profile(self.user, MediaTypes.TV.value)

        self.assertIn("drama", profile.genre_affinity)
