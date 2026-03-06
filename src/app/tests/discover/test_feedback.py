# ruff: noqa: D102

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from app.models import (
    DiscoverFeedback,
    DiscoverFeedbackType,
    Item,
    MediaTypes,
    Movie,
    Sources,
)


class DiscoverFeedbackModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="discover-feedback-user",
            password="testpass",
        )
        self.item = Item.objects.create(
            media_id="feedback-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Feedback Movie",
            image="https://example.com/feedback.jpg",
        )

    def test_discover_feedback_is_unique_per_user_item_type(self):
        DiscoverFeedback.objects.create(
            user=self.user,
            item=self.item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )

        with self.assertRaises(IntegrityError):
            DiscoverFeedback.objects.create(
                user=self.user,
                item=self.item,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            )

    def test_hidden_feedback_does_not_create_visible_media_entry(self):
        DiscoverFeedback.objects.create(
            user=self.user,
            item=self.item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )

        self.assertFalse(Movie.objects.filter(user=self.user, item=self.item).exists())
