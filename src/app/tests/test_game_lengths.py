import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from app.models import Item, MediaTypes, Sources
from app.services import game_lengths


def _mock_response(*, json_payload=None, text="", status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_payload
    response.raise_for_status.return_value = None
    return response


def _detail_html():
    payload = {
        "props": {
            "pageProps": {
                "game": {
                    "data": {
                        "game": [
                            {
                                "game_id": 160618,
                                "game_name": "Dispatch",
                                "profile_steam": 2592160,
                                "profile_itch": 0,
                                "profile_ign": "84fb8aca-cd19-4ff6-8919-c1b8ef5fa88a",
                                "release_world": "2025-10-22",
                                "comp_main_count": 1261,
                                "comp_main": 30748,
                                "comp_main_l": 22574,
                                "comp_main_h": 38054,
                                "comp_main_med": 30600,
                                "comp_plus_count": 364,
                                "comp_plus": 36825,
                                "comp_plus_l": 30987,
                                "comp_plus_h": 59813,
                                "comp_plus_med": 36000,
                                "comp_100_count": 108,
                                "comp_100": 71468,
                                "comp_100_l": 49204,
                                "comp_100_h": 113164,
                                "comp_100_med": 70201,
                                "comp_all_count": 1733,
                                "comp_all": 33301,
                                "comp_all_l": 23851,
                                "comp_all_h": 94081,
                                "comp_all_med": 31680,
                            },
                        ],
                        "platformData": [
                            {
                                "platform": "PC",
                                "count_comp": 1479,
                                "comp_main": 31109,
                                "comp_plus": 37435,
                                "comp_100": 72077,
                                "comp_low": 14400,
                                "comp_high": 154860,
                            },
                            {
                                "platform": "PlayStation 5",
                                "count_comp": 229,
                                "comp_main": 29726,
                                "comp_plus": 39396,
                                "comp_100": 77411,
                                "comp_low": 18000,
                                "comp_high": 104400,
                            },
                        ],
                    },
                },
            },
        },
    }
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}"
        "</script></body></html>"
    )


class GameLengthsServiceTests(TestCase):
    @patch("app.services.game_lengths.provider_services.session.get")
    def test_fetch_hltb_detail_extracts_summary_platforms_and_external_ids(self, mock_get):
        mock_get.return_value = _mock_response(text=_detail_html())

        payload = game_lengths.fetch_hltb_detail(160618)

        self.assertEqual(payload["game_id"], 160618)
        self.assertEqual(payload["summary"]["main_minutes"], 512)
        self.assertEqual(payload["summary"]["main_plus_minutes"], 614)
        self.assertEqual(payload["summary"]["completionist_minutes"], 1191)
        self.assertEqual(payload["external_ids"]["steam_app_id"], 2592160)
        self.assertEqual(payload["external_ids"]["ign_uuid"], "84fb8aca-cd19-4ff6-8919-c1b8ef5fa88a")
        self.assertEqual(payload["single_player_table"][0]["label"], "Main Story")
        self.assertEqual(payload["platform_table"][0]["platform"], "PC")
        self.assertEqual(payload["platform_table"][0]["fastest_minutes"], 240)

    @patch("app.services.game_lengths.fetch_hltb_detail")
    @patch("app.services.game_lengths.fetch_hltb_search")
    def test_resolve_hltb_candidate_prefers_direct_url(self, mock_search, mock_detail):
        mock_detail.return_value = {
            "game_id": 160618,
            "external_ids": {"steam_app_id": 2592160},
        }

        payload = game_lengths.resolve_hltb_candidate(
            {
                "title": "Dispatch",
                "details": {
                    "release_date": "2025-10-22",
                    "platforms": ["PC"],
                },
                "external_links": {
                    "HowLongToBeat": "https://howlongtobeat.com/game/160618",
                },
            },
        )

        self.assertEqual(payload["match"], "direct_url")
        self.assertEqual(payload["hltb_id"], 160618)
        mock_search.assert_not_called()

    @patch("app.services.game_lengths.fetch_hltb_detail")
    @patch("app.services.game_lengths.fetch_hltb_search")
    def test_resolve_hltb_candidate_picks_dispatch_and_rejects_dispatcher(self, mock_search, mock_detail):
        mock_search.return_value = {
            "data": [
                {
                    "game_id": 160618,
                    "game_name": "Dispatch",
                    "release_world": 2025,
                    "profile_platform": "PC, PlayStation 5",
                },
                {
                    "game_id": 31900,
                    "game_name": "Dispatcher",
                    "release_world": 2015,
                    "profile_platform": "PC",
                },
            ],
        }
        mock_detail.return_value = {
            "game_id": 160618,
            "external_ids": {"steam_app_id": 2592160},
        }

        payload = game_lengths.resolve_hltb_candidate(
            {
                "title": "Dispatch",
                "details": {
                    "release_date": "2025-10-22",
                    "platforms": ["PC"],
                },
                "external_ids": {"steam_app_id": "2592160"},
            },
        )

        self.assertEqual(payload["hltb_id"], 160618)
        self.assertEqual(payload["match"], "steam_verified")

    @patch("app.services.game_lengths.fetch_igdb_time_to_beat")
    @patch("app.services.game_lengths.resolve_hltb_candidate")
    def test_refresh_game_lengths_stores_igdb_fallback_when_hltb_is_ambiguous(
        self,
        mock_resolve,
        mock_igdb_time,
    ):
        item = Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
        )
        mock_resolve.return_value = {"match": "ambiguous"}
        mock_igdb_time.return_value = {
            "game_id": 325609,
            "summary": {
                "hastily_seconds": 32400,
                "normally_seconds": 32400,
                "completely_seconds": 46800,
                "count": 13,
            },
            "raw": [{"game_id": 325609}],
        }

        payload = game_lengths.refresh_game_lengths(
            item,
            igdb_metadata={
                "title": "Dispatch",
                "details": {"release_date": "2025-10-22", "platforms": ["PC"]},
                "external_links": {
                    "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
                },
            },
            force=True,
        )
        item.refresh_from_db()

        self.assertEqual(payload["active_source"], "igdb")
        self.assertEqual(item.provider_game_lengths_source, "igdb")
        self.assertEqual(item.provider_game_lengths_match, "ambiguous")
        self.assertNotIn("hltb", item.provider_game_lengths)
        self.assertIsNone(item.provider_external_ids.get("hltb_game_id"))
        self.assertEqual(
            item.provider_game_lengths["igdb"]["summary"]["normally_seconds"],
            32400,
        )

    @patch("app.services.game_lengths.igdb.get_access_token", return_value="token")
    @patch("app.services.game_lengths.provider_services.api_request")
    def test_fetch_igdb_time_to_beat_normalizes_response(self, mock_api_request, _mock_token):
        mock_api_request.return_value = [
            {
                "game_id": 325609,
                "hastily": 32400,
                "normally": 32400,
                "completely": 46800,
                "count": 13,
            },
        ]

        payload = game_lengths.fetch_igdb_time_to_beat(325609)

        self.assertEqual(payload["game_id"], 325609)
        self.assertEqual(payload["summary"]["hastily_seconds"], 32400)
        self.assertEqual(payload["summary"]["normally_seconds"], 32400)
        self.assertEqual(payload["summary"]["completely_seconds"], 46800)
        self.assertEqual(payload["summary"]["count"], 13)
        self.assertEqual(payload["raw"][0]["game_id"], 325609)
