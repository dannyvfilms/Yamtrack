
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
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

        # Verify the record doesn't exist in the historical table directly
        from django.apps import apps
        HistoricalMovie = apps.get_model("app", "HistoricalMovie")
        self.assertFalse(
            HistoricalMovie.objects.filter(history_id=self.history_id).exists(),
        )

    def test_delete_nonexistent_history_record(self):
        """Test deleting a nonexistent history record."""
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

    def test_delete_history_record_verification(self):
        """Test that deletion verification works correctly."""
        # Create another history entry
        self.movie.status = Status.PAUSED.value
        self.movie.save()
        second_history = self.movie.history.first()
        second_history_id = second_history.history_id
        second_history.history_user = self.user
        second_history.save()

        # Verify both records exist
        self.assertEqual(
            self.movie.history.filter(history_id=self.history_id).count(),
            1,
        )
        self.assertEqual(
            self.movie.history.filter(history_id=second_history_id).count(),
            1,
        )

        # Delete the first record
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

        # Verify only the first record is deleted
        self.assertEqual(
            self.movie.history.filter(history_id=self.history_id).count(),
            0,
        )
        self.assertEqual(
            self.movie.history.filter(history_id=second_history_id).count(),
            1,
        )


