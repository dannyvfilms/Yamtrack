from django.test import SimpleTestCase

from app.discover.registry import ALL_MEDIA_KEY, get_rows
from app.models import MediaTypes


class DiscoverRegistryTests(SimpleTestCase):
    def test_all_media_rows_are_composed_in_service(self):
        self.assertEqual(get_rows(ALL_MEDIA_KEY), [])

    def test_tv_and_anime_use_extended_six_row_layout(self):
        expected_keys = [
            "trending_right_now",
            "all_time_greats_unseen",
            "coming_soon",
            "top_picks_for_you",
            "clear_out_next",
            "comfort_rewatches",
        ]
        for media_type in [MediaTypes.TV.value, MediaTypes.ANIME.value]:
            with self.subTest(media_type=media_type):
                keys = [row.key for row in get_rows(media_type)]
                self.assertEqual(keys, expected_keys)

    def test_remaining_media_types_keep_standard_five_row_layout(self):
        expected_keys = [
            "trending_right_now",
            "all_time_greats_unseen",
            "coming_soon",
            "top_picks_for_you",
            "comfort_rewatches",
        ]
        media_types = [
            MediaTypes.MOVIE.value,
            MediaTypes.MUSIC.value,
            MediaTypes.PODCAST.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        ]
        for media_type in media_types:
            with self.subTest(media_type=media_type):
                keys = [row.key for row in get_rows(media_type)]
                self.assertEqual(keys, expected_keys)
