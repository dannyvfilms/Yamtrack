from django.contrib.auth import get_user_model
from django.test import TestCase

from app.columns import resolve_columns
from app.models import MediaTypes


class ResolveColumnsTests(TestCase):
    """Tests for table column resolution and preference handling."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="columns-user",
            password="12345",
        )

    def test_tv_time_left_columns(self):
        columns = resolve_columns(
            media_type=MediaTypes.TV.value,
            current_sort="time_left",
            user=self.user,
            table_type="media",
        )
        keys = [column.key for column in columns]

        self.assertIn("episodes_left", keys)
        self.assertIn("time_left", keys)
        self.assertIn("runtime", keys)
        self.assertIn("time_watched", keys)
        self.assertNotIn("progress", keys)
        self.assertNotIn("last_watched", keys)

    def test_movie_columns_do_not_include_progress(self):
        columns = resolve_columns(
            media_type=MediaTypes.MOVIE.value,
            current_sort="score",
            user=self.user,
            table_type="media",
        )
        keys = [column.key for column in columns]

        self.assertNotIn("progress", keys)
        self.assertNotIn("episodes_left", keys)
        self.assertNotIn("time_left", keys)
        self.assertIn("runtime", keys)
        self.assertIn("time_watched", keys)
        self.assertIn("popularity", keys)
        self.assertNotIn("time_to_beat", keys)

    def test_list_table_inherits_media_columns_and_adds_media_type(self):
        columns = resolve_columns(
            media_type=MediaTypes.MOVIE.value,
            current_sort="score",
            user=self.user,
            table_type="list",
        )
        keys = [column.key for column in columns]

        self.assertEqual(keys[:3], ["image", "title", "media_type"])
        self.assertIn("runtime", keys)
        self.assertIn("time_watched", keys)
        self.assertIn("popularity", keys)
        self.assertIn("status", keys)
        self.assertNotIn("progress", keys)

    def test_game_columns_include_time_to_beat(self):
        columns = resolve_columns(
            media_type=MediaTypes.GAME.value,
            current_sort="score",
            user=self.user,
            table_type="media",
        )
        keys = [column.key for column in columns]

        self.assertIn("progress", keys)
        self.assertIn("time_to_beat", keys)
        self.assertNotIn("time_left", keys)
        self.assertNotIn("runtime", keys)

    def test_anime_columns_include_runtime(self):
        columns = resolve_columns(
            media_type=MediaTypes.ANIME.value,
            current_sort="score",
            user=self.user,
            table_type="media",
        )
        keys = [column.key for column in columns]

        self.assertIn("progress", keys)
        self.assertIn("runtime", keys)
        self.assertIn("time_watched", keys)
        self.assertIn("popularity", keys)
        self.assertNotIn("time_left", keys)

    def test_artist_table_uses_artist_name_column(self):
        columns = resolve_columns(
            media_type=MediaTypes.MUSIC.value,
            current_sort="score",
            user=self.user,
            table_type="artist",
        )
        keys = [column.key for column in columns]

        self.assertIn("artist_name", keys)
        self.assertNotIn("title", keys)

    def test_resolve_columns_is_deterministic(self):
        keys_first = [
            column.key
            for column in resolve_columns(
                media_type=MediaTypes.TV.value,
                current_sort="score",
                user=self.user,
                table_type="media",
            )
        ]
        keys_second = [
            column.key
            for column in resolve_columns(
                media_type=MediaTypes.TV.value,
                current_sort="score",
                user=self.user,
                table_type="media",
            )
        ]

        self.assertEqual(keys_first, keys_second)

    def test_user_prefs_reorder_hide_and_unknown_keys(self):
        self.user.table_column_prefs = {
            MediaTypes.MOVIE.value: {
                "order": ["status", "legacy_column"],
                "hidden": ["score", "image", "legacy_column"],
            },
        }
        self.user.save(update_fields=["table_column_prefs"])

        columns = resolve_columns(
            media_type=MediaTypes.MOVIE.value,
            current_sort="score",
            user=self.user,
            table_type="media",
        )
        keys = [column.key for column in columns]

        # Fixed columns stay first, and hidden only applies to hideable columns.
        self.assertEqual(
            keys,
            [
                "image",
                "title",
                "status",
                "runtime",
                "time_watched",
                "popularity",
                "release_date",
                "date_added",
                "start_date",
                "end_date",
            ],
        )

    def test_new_columns_append_when_not_in_saved_order(self):
        self.user.table_column_prefs = {
            MediaTypes.MOVIE.value: {
                "order": ["status"],
                "hidden": [],
            },
        }
        self.user.save(update_fields=["table_column_prefs"])

        columns = resolve_columns(
            media_type=MediaTypes.MOVIE.value,
            current_sort="score",
            user=self.user,
            table_type="media",
        )
        keys = [column.key for column in columns]

        self.assertEqual(
            keys,
            [
                "image",
                "title",
                "status",
                "score",
                "runtime",
                "time_watched",
                "popularity",
                "release_date",
                "date_added",
                "start_date",
                "end_date",
            ],
        )
