from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.translation import override

from app.models import Item, MediaTypes, Sources
from app.templatetags import app_tags
from users.models import TitleDisplayPreferenceChoices


class ItemTitlePreferenceTests(TestCase):
    """Tests for item title preference resolution."""

    def _build_item(self, title="Localized", original_title="Original", localized_title="Localized"):
        return Item(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            original_title=original_title,
            localized_title=localized_title,
            image="https://example.com/poster.jpg",
        )

    def test_resolve_title_preference_localized(self):
        item = self._build_item()
        display, alternative = item.resolve_title_preference(
            TitleDisplayPreferenceChoices.LOCALIZED,
        )
        self.assertEqual(display, "Localized")
        self.assertEqual(alternative, "Original")

    def test_resolve_title_preference_original(self):
        item = self._build_item()
        display, alternative = item.resolve_title_preference(
            TitleDisplayPreferenceChoices.ORIGINAL,
        )
        self.assertEqual(display, "Original")
        self.assertEqual(alternative, "Localized")

    def test_resolve_title_preference_auto_prefers_localized(self):
        item = self._build_item()
        display, alternative = item.resolve_title_preference(
            TitleDisplayPreferenceChoices.AUTO,
        )
        self.assertEqual(display, "Localized")
        self.assertEqual(alternative, "Original")

    def test_resolve_title_preference_hides_non_latin_alternative_for_english_locale(self):
        item = self._build_item(
            title="The Sound of Music",
            original_title="サウンド・オブ・ミュージック",
            localized_title="The Sound of Music",
        )

        with override("en"):
            display, alternative = item.resolve_title_preference(
                TitleDisplayPreferenceChoices.LOCALIZED,
            )

        self.assertEqual(display, "The Sound of Music")
        self.assertIsNone(alternative)

    def test_resolve_title_preference_falls_back_when_original_missing(self):
        item = self._build_item(original_title=None, localized_title="Localized")
        display, alternative = item.resolve_title_preference(
            TitleDisplayPreferenceChoices.ORIGINAL,
        )
        self.assertEqual(display, "Localized")
        self.assertIsNone(alternative)

    def test_title_fields_from_metadata_normalizes_structured_payloads(self):
        title_fields = Item.title_fields_from_metadata(
            {
                "title": {"language": "jpn", "name": "Sodo Ato Onrain"},
                "original_title": "{'language': 'jpn', 'name': 'Sodo Ato Onrain'}",
                "localized_title": None,
            },
        )

        self.assertEqual(title_fields["title"], "Sodo Ato Onrain")
        self.assertEqual(title_fields["original_title"], "Sodo Ato Onrain")
        self.assertEqual(title_fields["localized_title"], "Sodo Ato Onrain")

    def test_get_display_and_alternative_title_uses_user_preference(self):
        user = get_user_model().objects.create_user(
            username="pref-user",
            password="password",
            title_display_preference=TitleDisplayPreferenceChoices.ORIGINAL,
        )
        item = self._build_item()
        display, alternative = item.get_display_and_alternative_title(user=user)
        self.assertEqual(display, "Original")
        self.assertEqual(alternative, "Localized")


class TitleTemplateFilterTests(TestCase):
    """Tests for display_title and alternative_title template filters."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="template-user",
            password="password",
            title_display_preference=TitleDisplayPreferenceChoices.ORIGINAL,
        )
        self.item = Item(
            media_id="99",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Localized",
            original_title="Original",
            localized_title="Localized",
            image="https://example.com/poster.jpg",
        )

    def test_display_title_filter(self):
        self.assertEqual(app_tags.display_title(self.item, self.user), "Original")

    def test_alternative_title_filter(self):
        self.assertEqual(app_tags.alternative_title(self.item, self.user), "Localized")

    def test_alternative_title_filter_hides_equivalent_variants(self):
        item = Item(
            media_id="100",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Pokemon",
            original_title="Pokémon",
            localized_title="Pokemon",
            image="https://example.com/poster.jpg",
        )

        with override("en"):
            self.assertIsNone(app_tags.alternative_title(item, self.user))

    def test_display_title_filter_normalizes_structured_provider_payloads(self):
        payload = {
            "title": {"language": "jpn", "name": "Sōdo Āto Onrain"},
            "original_title": {"language": "jpn", "name": "Sōdo Āto Onrain"},
            "localized_title": {"language": "jpn", "name": "Sōdo Āto Onrain"},
        }

        self.assertEqual(app_tags.display_title(payload, self.user), "Sōdo Āto Onrain")

    def test_display_title_filter_normalizes_stringified_payloads(self):
        payload = {
            "title": "{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}",
            "original_title": "{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}",
            "localized_title": "{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}",
        }

        self.assertEqual(app_tags.display_title(payload, self.user), "Sōdo Āto Onrain")
