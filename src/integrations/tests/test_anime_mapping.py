from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from integrations import anime_mapping


class AnimeMappingTests(TestCase):
    """Tests for shared anime mapping loader behavior."""

    @patch("integrations.anime_mapping.services.api_request")
    def test_load_mapping_data_uses_fixture_in_test_mode(self, mock_api_request):
        """Tests should use the bundled anime-mapping fixture instead of network calls."""
        cache.clear()

        result = anime_mapping.load_mapping_data()

        self.assertIn("anime_episode", result)
        self.assertEqual(result["anime_episode"]["mal_id"], "52991")
        mock_api_request.assert_not_called()
