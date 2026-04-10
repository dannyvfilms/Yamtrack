from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class StaticCssContractTests(SimpleTestCase):
    """Protect the canonical compiled CSS path from drifting."""

    def setUp(self):
        """Resolve the shared asset paths used by the regression checks."""
        base_dir = Path(settings.BASE_DIR)
        self.tailwind_css_path = base_dir / "static" / "css" / "tailwind.css"
        self.base_template_path = base_dir / "templates" / "base.html"
        self.base_public_template_path = base_dir / "templates" / "base_public.html"
        self.service_worker_path = base_dir / "static" / "js" / "serviceworker.js"

    def test_source_static_tree_does_not_include_tailwind_css(self):
        """The source static tree should not ship the retired duplicate CSS file."""
        self.assertFalse(
            self.tailwind_css_path.exists(),
            (
                "static/css/tailwind.css should not exist; "
                "use static/css/main.css instead."
            ),
        )

    def test_shared_templates_and_service_worker_reference_main_css_only(self):
        """Shared entrypoints should reference main.css and never the retired path."""
        for path in (
            self.base_template_path,
            self.base_public_template_path,
            self.service_worker_path,
        ):
            content = path.read_text(encoding="utf-8")
            relative_path = path.relative_to(settings.BASE_DIR).as_posix()

            self.assertIn(
                "css/main.css",
                content,
                f"{relative_path} should reference css/main.css.",
            )
            self.assertNotIn(
                "css/tailwind.css",
                content,
                f"{relative_path} should not reference css/tailwind.css.",
            )
