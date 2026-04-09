import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class WebManifestTests(SimpleTestCase):
    """Regression tests for the installed PWA manifest."""

    def setUp(self):
        self.static_dir = Path(settings.BASE_DIR) / "static"
        manifest_path = self.static_dir / "favicon" / "site.webmanifest"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def test_manifest_includes_maskable_icons_and_valid_shortcuts(self):
        icons = {icon["src"]: icon for icon in self.manifest["icons"]}
        expected_icons = {
            "/static/favicon/android-chrome-192x192.png": "any",
            "/static/favicon/android-chrome-512x512.png": "any",
            "/static/favicon/android-chrome-192x192-maskable.png": "maskable",
            "/static/favicon/android-chrome-512x512-maskable.png": "maskable",
        }

        for src, purpose in expected_icons.items():
            self.assertIn(src, icons)
            self.assertEqual(icons[src]["purpose"], purpose)
            asset_path = self.static_dir / src.removeprefix("/static/")
            self.assertTrue(asset_path.exists(), f"Missing manifest icon asset: {src}")

        expected_shortcuts = {
            "Home": ("/", "/static/img/shortcuts/home.svg"),
            "TV Shows": ("/medialist/tv", "/static/img/shortcuts/tv.svg"),
            "Movies": ("/medialist/movie", "/static/img/shortcuts/movies.svg"),
            "Anime": ("/medialist/anime", "/static/img/shortcuts/anime.svg"),
            "Manga": ("/medialist/manga", "/static/img/shortcuts/manga.svg"),
            "Games": ("/medialist/game", "/static/img/shortcuts/games.svg"),
            "Books": ("/medialist/book", "/static/img/shortcuts/books.svg"),
            "Comics": ("/medialist/comic", "/static/img/shortcuts/comics.svg"),
            "Board Games": ("/medialist/boardgame", "/static/img/shortcuts/boardgames.svg"),
            "Statistics": ("/statistics", "/static/img/shortcuts/stats.svg"),
            "Your Lists": ("/lists", "/static/img/shortcuts/lists.svg"),
        }
        shortcuts = {shortcut["name"]: shortcut for shortcut in self.manifest["shortcuts"]}

        self.assertEqual(set(shortcuts), set(expected_shortcuts))

        for name, (url, icon_src) in expected_shortcuts.items():
            shortcut = shortcuts[name]
            self.assertEqual(shortcut["url"], url)
            self.assertEqual(shortcut["icons"], [{"src": icon_src, "sizes": "192x192"}])
            asset_path = self.static_dir / icon_src.removeprefix("/static/")
            self.assertTrue(asset_path.exists(), f"Missing shortcut icon asset: {icon_src}")
