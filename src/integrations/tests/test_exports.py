import csv
from datetime import UTC, datetime
from io import StringIO

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import TestCase
from django.urls import reverse

from app.mixins import disable_fetch_releases
from app.models import (
    Anime,
    Book,
    Episode,
    Game,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from lists.models import CustomList, CustomListItem


class ExportCSVTest(TestCase):
    """Test exporting media to CSV."""

    def setUp(self):
        """Create necessary data for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        self.client.login(**self.credentials)

        with disable_fetch_releases():
            item_movie = Item.objects.create(
                media_id="10494",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title="Perfect Blue",
                image="https://image.url",
            )
            Movie.objects.create(
                item=item_movie,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                notes="Nice",
                start_date=datetime(2023, 6, 1, 0, 0, tzinfo=UTC),
                end_date=datetime(2023, 6, 1, 0, 0, tzinfo=UTC),
            )

            item_season = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Friends",
                image="https://image.url",
                season_number=1,
            )

            season = Season.objects.create(
                item=item_season,
                user=self.user,
                score=9,
                status=Status.IN_PROGRESS.value,
                notes="Nice",
            )

            item_episode = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Friends",
                image="https://image.url",
                season_number=1,
                episode_number=1,
            )
            Episode.objects.create(
                item=item_episode,
                related_season=season,
                end_date=datetime(2023, 6, 1, 0, 0, tzinfo=UTC),
            )

            item_anime = Item.objects.create(
                media_id="1",
                source=Sources.MAL.value,
                media_type=MediaTypes.ANIME.value,
                title="Cowboy Bebop",
                image="https://image.url",
            )
            Anime.objects.create(
                item=item_anime,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=2,
                start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
            )

            item_manga = Item.objects.create(
                media_id="1",
                source=Sources.MAL.value,
                media_type=MediaTypes.MANGA.value,
                title="Berserk",
                image="https://image.url",
            )
            Manga.objects.create(
                item=item_manga,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=2,
                start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
            )

            item_game = Item.objects.create(
                media_id="1",
                source=Sources.IGDB.value,
                media_type=MediaTypes.GAME.value,
                title="The Witcher 3: Wild Hunt",
                image="https://image.url",
            )
            Game.objects.create(
                item=item_game,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=120,
                start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
            )

            item_book = Item.objects.create(
                media_id="OL21733390M",
                source=Sources.OPENLIBRARY.value,
                media_type=MediaTypes.BOOK.value,
                title="Fantastic Mr. Fox",
                image="https://image.url",
            )
            Book.objects.create(
                item=item_book,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=120,
                start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
            )

            custom_list = CustomList.objects.create(
                name="Favorites",
                description="Top picks",
                owner=self.user,
                visibility="private",
            )
            CustomListItem.objects.create(
                custom_list=custom_list,
                item=item_movie,
                added_by=self.user,
            )
            CustomListItem.objects.create(
                custom_list=custom_list,
                item=item_episode,
                added_by=self.user,
            )

    def test_export_csv(self):
        """Basic test exporting media to CSV."""
        # Generate the CSV file by accessing the export view
        response = self.client.get(reverse("export_csv"))

        # Assert that the response is successful (status code 200)
        self.assertEqual(response.status_code, 200)

        # Assert that the response content type is text/csv
        self.assertEqual(response["Content-Type"], "text/csv")

        # Read the streaming content and decode it
        content = b"".join(response.streaming_content).decode("utf-8")

        # Create a CSV reader from the CSV content
        reader = csv.DictReader(StringIO(content))

        db_media_ids = set(
            Item.objects.filter(
                Q(tv__user=self.user)
                | Q(movie__user=self.user)
                | Q(season__user=self.user)
                | Q(episode__related_season__user=self.user)
                | Q(anime__user=self.user)
                | Q(manga__user=self.user)
                | Q(game__user=self.user)
                | Q(book__user=self.user),
            ).values_list("media_id", flat=True),
        )

        list_rows = []
        list_item_rows = []

        # Verify each row in the CSV exists in the database
        for row in reader:
            row_type = row.get("row_type") or "media"
            if row_type == "list":
                list_rows.append(row)
                continue
            if row_type == "list_item":
                list_item_rows.append(row)
                continue

            media_id = row["media_id"]
            self.assertIn(media_id, db_media_ids)

        self.assertEqual(len(list_rows), 1)
        self.assertEqual(list_rows[0]["list_name"], "Favorites")
        self.assertEqual(len(list_item_rows), 2)
        self.assertTrue(all(r["list_name"] == "Favorites" for r in list_item_rows))
        exported_media_types = {r["media_type"] for r in list_item_rows}
        self.assertIn("movie", exported_media_types)
        self.assertIn("episode", exported_media_types)

    def test_export_csv_includes_item_entries_for_each_list_membership(self):
        """Items that appear on multiple lists should be exported once per list item."""
        movie_item = Item.objects.get(media_id="10494", media_type=MediaTypes.MOVIE.value)
        second_list = CustomList.objects.create(
            name="Rewatch",
            description="Need to revisit",
            owner=self.user,
            visibility="private",
        )
        CustomListItem.objects.create(
            custom_list=second_list,
            item=movie_item,
            added_by=self.user,
        )

        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 200)

        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))

        movie_list_item_rows = [
            row
            for row in reader
            if (row.get("row_type") or "media") == "list_item"
            and row.get("media_id") == movie_item.media_id
            and row.get("media_type") == movie_item.media_type
        ]

        exported_list_names = sorted({row["list_name"] for row in movie_list_item_rows})
        self.assertEqual(exported_list_names, ["Favorites", "Rewatch"])
