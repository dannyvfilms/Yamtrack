from django.test import SimpleTestCase

from app.discover.registry import ALL_MEDIA_KEY, get_rows
from app.models import MediaTypes


class DiscoverRegistryTests(SimpleTestCase):
    def test_all_media_rows_are_composed_in_service(self):
        self.assertEqual(get_rows(ALL_MEDIA_KEY), [])

    def test_each_media_type_uses_standard_five_row_layout(self):
        expected_keys = [
            "trending_right_now",
            "all_time_greats_unseen",
            "coming_soon",
            "top_picks_for_you",
            "comfort_rewatches",
        ]
        media_types = [
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
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
