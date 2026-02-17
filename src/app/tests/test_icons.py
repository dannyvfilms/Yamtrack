from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class IconTemplateTests(SimpleTestCase):
    """Regression tests for shared icon template validity."""

    def test_icons_do_not_use_auto_svg_width(self):
        icons_dir = Path(settings.BASE_DIR) / "templates" / "app" / "icons"
        bad_files = []

        for icon_path in sorted(icons_dir.rglob("*.svg")):
            content = icon_path.read_text(encoding="utf-8")
            if 'width="auto"' in content:
                bad_files.append(icon_path.relative_to(settings.BASE_DIR).as_posix())

        self.assertEqual(
            bad_files,
            [],
            f'Found icons with invalid width="auto": {bad_files}',
        )
