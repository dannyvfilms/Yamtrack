from unittest.mock import patch

from django.test import TestCase

from app.models import Item, MediaTypes, Sources
from app.services import tracking_hydration


class TrackingHydrationTests(TestCase):
    @patch("app.services.tracking_hydration.credits.sync_item_credits_from_metadata")
    @patch("app.services.tracking_hydration.upsert_provider_links")
    @patch("app.services.tracking_hydration.services.get_media_metadata")
    def test_ensure_item_metadata_preserves_tmdb_tv_anime_genre_supplement(
        self,
        mock_get_media_metadata,
        _mock_upsert_provider_links,
        _mock_sync_item_credits,
    ):
        item = Item.objects.create(
            media_id="2001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Tracked Anime Show",
            image="https://example.com/tracked-anime-show.jpg",
            genres=["Comedy", "Anime"],
        )
        mock_get_media_metadata.return_value = {
            "media_id": item.media_id,
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "title": item.title,
            "image": item.image,
            "genres": ["Comedy"],
            "details": {},
            "related": {},
        }

        result = tracking_hydration.ensure_item_metadata(
            None,
            MediaTypes.TV.value,
            item.media_id,
            Sources.TMDB.value,
        )

        item.refresh_from_db()
        self.assertFalse(result.created)
        self.assertEqual(item.genres, ["Comedy", "Anime"])
