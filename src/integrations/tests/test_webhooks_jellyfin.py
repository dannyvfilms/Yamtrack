import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from app import live_playback
from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    ItemProviderLink,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from integrations.webhooks.jellyfin import JellyfinWebhookProcessor

TVDB_EPISODE_IDS: dict = {
    "friends_s1e1": 303821
}
TMDB_TV_IDS: dict = {
    "friends": 1668
}
JELLYFIN_STOP_PAYLOADS: dict = {
    "friends_s1e1_unplayed": {
        "Event": "Stop",
        "Item": {
            "Type": "Episode",
            "Name": "The One Where Monica Gets a Roommate",
            "ProviderIds": {"Tvdb": str(TVDB_EPISODE_IDS["friends_s1e1"]), "Imdb": "tt0583459"},
            "UserData": {"Played": False},
            "SeriesName": "Friends",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
        },
    }
}
JELLYFIN_STOP_PAYLOADS["friends_s1e1_played"] = {
    **JELLYFIN_STOP_PAYLOADS["friends_s1e1_unplayed"],
    "Item": {
        **JELLYFIN_STOP_PAYLOADS["friends_s1e1_unplayed"]["Item"],
        "UserData": {"Played": True},
    },
}
TVDB_TV_IDS: dict = {
    "friends": 79168
}
# Additional test data for non-mocked tests
TMDB_EXTERNAL_IDS_RETURN_VALUE: dict = {
    "friends": {
        "tvdb_id": TVDB_TV_IDS["friends"],
    },
}

# Mock data for Breaking Bad tests
BREAKING_BAD_TMDB_FIND_RETURN_VALUE: dict = {
    "tmdb_id": {"show_id": 1396, "season_number": 1, "episode_number": 1},
    "tv_results": [],
}

BREAKING_BAD_TV_DETAILS_RETURN_VALUE: dict = {
    "media_id": "1396",
    "source": "tmdb",
    "source_url": "https://www.themoviedb.org/tv/1396",
    "media_type": "tv",
    "title": "Breaking Bad",
    "original_title": "Breaking Bad",
    "localized_title": "Breaking Bad",
    "max_progress": 62,
    "image": "https://example.com/breaking-bad.jpg",
    "synopsis": "",
    "genres": [],
    "score": 0.0,
    "score_count": 0,
    "details": {
        "format": "TV",
        "first_air_date": None,
        "last_air_date": None,
        "status": None,
        "seasons": None,
        "episodes": 62,
        "runtime": None,
        "studios": [],
        "country": [],
        "languages": [],
    },
    "cast": [],
    "crew": [],
    "studios_full": [],
    "related": {
        "seasons": [],
        "recommendations": [],
    },
    "tvdb_id": "81189",
    "external_links": {},
    "last_episode_season": None,
    "next_episode_season": None,
    "providers": {},
}

BREAKING_BAD_MAL_ANIME_RETURN_VALUE: dict = {
    "media_id": "4501",
    "source": "mal",
    "source_url": "https://myanimelist.net/anime/4501",
    "media_type": "anime",
    "title": "Breaking Bad",
    "original_title": "Breaking Bad",
    "localized_title": "Breaking Bad",
    "max_progress": 62,
    "image": "https://example.com/breaking-bad.jpg",
    "synopsis": "",
    "genres": [],
    "score": 0.0,
    "score_count": 0,
    "details": {
        "format": None,
        "start_date": None,
        "end_date": None,
        "status": None,
        "episodes": 62,
        "runtime": None,
        "studios": [],
        "season": None,
        "broadcast": None,
        "source": None,
    },
    "related": {
        "related_anime": [],
        "recommendations": [],
    },
}

# Mock data for Matrix movie tests
MATRIX_TMDB_FIND_RETURN_VALUE: dict = {
    "movie_results": [{"id": 603}],
    "tv_results": [],
}

MATRIX_TV_DETAILS_RETURN_VALUE: dict = {
    "media_id": "603",
    "title": "The Matrix",
    "image": "https://example.com/matrix.jpg",
    "release_date": "1999-03-30",
}

# Mock data for Frieren anime tests
FRIEREN_TMDB_FIND_RETURN_VALUE: dict = {
    "tv_episode_results": [{"show_id": 52991, "season_number": 1, "episode_number": 1}],
    "tv_results": [],
}

FRIEREN_MAL_ANIME_RETURN_VALUE: dict = {
    "media_id": "52991",
    "source": "mal",
    "source_url": "https://myanimelist.net/anime/52991",
    "media_type": "anime",
    "title": "Frieren: Beyond Journey's End",
    "original_title": "Frieren: Beyond Journey's End",
    "localized_title": "Frieren: Beyond Journey's End",
    "max_progress": 28,
    "image": "https://example.com/frieren.jpg",
    "synopsis": "",
    "genres": [],
    "score": 0.0,
    "score_count": 0,
    "details": {
        "format": None,
        "start_date": None,
        "end_date": None,
        "status": None,
        "episodes": 28,
        "runtime": None,
        "studios": [],
        "season": None,
        "broadcast": None,
        "source": None,
    },
    "related": {
        "related_anime": [],
        "recommendations": [],
    },
}

FRIEREN_TV_DETAILS_RETURN_VALUE: dict = {
    "media_id": "209867",
    "source": "tmdb",
    "source_url": "https://www.themoviedb.org/tv/209867",
    "media_type": "tv",
    "title": "Frieren: Beyond Journey's End",
    "original_title": "Frieren: Beyond Journey's End",
    "localized_title": "Frieren: Beyond Journey's End",
    "max_progress": 28,
    "image": "https://example.com/frieren.jpg",
    "synopsis": "",
    "genres": ["Animation"],
    "score": 0.0,
    "score_count": 0,
    "details": {
        "format": "TV",
        "first_air_date": None,
        "last_air_date": None,
        "status": None,
        "seasons": None,
        "episodes": 28,
        "runtime": None,
        "studios": [],
        "country": [],
        "languages": [],
    },
    "cast": [],
    "crew": [],
    "studios_full": [],
    "related": {
        "seasons": [],
        "recommendations": [],
    },
    "tvdb_id": "424536",
    "external_links": {},
    "last_episode_season": None,
    "next_episode_season": None,
    "providers": {},
}

# https://developer.themoviedb.org/reference/find-by-id
TMDB_FIND_RETURN_VALUE: dict = {
    "friends_s1e1": {
        "movie_results": [],
        "person_results": [],
        "tv_results": [],
        "tv_episode_results": [
            {
            "id": 85987,
            "name": "Pilot",
            "overview": "After Rachel leaves her fiancé at the altar, ...",
            "media_type": "tv_episode",
            "vote_average": 7.142,
            "vote_count": 141,
            "air_date": "1994-09-22",
            "episode_number": 1,
            "episode_type": "standard",
            "production_code": "456650",
            "runtime": 23,
            "season_number": 1,
            "show_id": TMDB_TV_IDS["friends"],
            "still_path": "/Slm6IczgHJWpR4dIv33IRtNrq5.jpg"
            }
        ],
        "tv_season_results": []
    }
}
# https://developer.themoviedb.org/reference/tv-series-details - long reply, simplified
# series_id=1668
# append_to_response=recommendations,external_ids,aggregate_credits,alternative_titles,watch/providers
TMDB_TV_DETAILS_RETURN_VALUE: dict = {
    "friends": {
        "media_id": str(TMDB_TV_IDS["friends"]),
        "source": "tmdb",
        "source_url": f"https://www.themoviedb.org/tv/{TMDB_TV_IDS['friends']}",
        "media_type": "tv",
        "title": "Friends",
        "original_title": "Friends",
        "localized_title": "Friends",
        "max_progress": 228,
        "image": "https://image.tmdb.org/t/p/w500/2koX1xLkpTQM4IZebYvKysFW1Nh.jpg",
        "synopsis": "",
        "genres": [],
        "score": 7.9,
        "score_count": 110.6843,
        "details": {
            "format": "TV",
            "first_air_date": "1994-09-22",
            "last_air_date": None,
            "status": None,
            "seasons": 10,
            "episodes": 228,
            "runtime": None,
            "studios": [],
            "country": [],
            "languages": [],
        },
        "cast": [],
        "crew": [],
        "studios_full": [],
        "related": {
            "seasons": [],
            "recommendations": [],
        },
        "tvdb_id": "79168",
        "external_links": {},
        "last_episode_season": None,
        "next_episode_season": None,
        "providers": {},
    }
}


class JellyfinWebhookTests(TestCase):
    """Tests for Jellyfin webhook."""

    TEST_USERNAME = "testuser"
    TEST_PASSWORD = "test-password"  # noqa: S105
    TEST_TOKEN = "test-token"  # noqa: S105

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.user = get_user_model().objects.create_superuser(
            username=self.TEST_USERNAME,
            password=self.TEST_PASSWORD,
        )
        self.user.token = self.TEST_TOKEN
        self.user.save(update_fields=["token"])
        self.url = reverse("jellyfin_webhook", kwargs={"token": self.TEST_TOKEN})

    def tearDown(self):
        """Clear cached playback state and reset Jellyfin settings."""
        live_playback.clear_user_playback_state(self.user.id)
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = False
        self.user.save(update_fields=[
            "jellyfin_provider_priority_enabled",
            "jellyfin_match_existing_enabled",
        ])

    def test_invalid_token(self):
        """Test webhook with invalid token returns 401."""
        url = reverse("jellyfin_webhook", kwargs={"token": "invalid-token"})
        response = self.client.post(url, data={}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_tv_episode_mark_played(self):
        """Test webhook handles TV episode mark played event."""
        payload = JELLYFIN_STOP_PAYLOADS["friends_s1e1_played"]

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify objects were created
        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id=str(TMDB_TV_IDS["friends"]))
        self.assertEqual(tv_item.title, "Friends")

        tv = TV.objects.get(item=tv_item, user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(
            item__media_id=str(TMDB_TV_IDS["friends"]),
            item__season_number=1,
        )
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        episode = Episode.objects.get(
            item__media_id=str(TMDB_TV_IDS["friends"]),
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)

    def test_movie_mark_played(self):
        """Test webhook handles movie mark played event."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify movie was created and marked as completed
        movie = Movie.objects.get(
            item__media_id="603",
            user=self.user,
        )
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.find")
    def test_anime_movie_mark_played(self, mock_tmdb_find, mock_mal_anime):
        """Test webhook handles movie mark played event."""
        mock_tmdb_find.return_value = {
            "movie_results": [{"id": 10494}],
        }
        mock_mal_anime.return_value = {
            "media_id": "437",
            "source": "mal",
            "source_url": "https://myanimelist.net/anime/437",
            "media_type": "anime",
            "title": "Perfect Blue",
            "original_title": "Perfect Blue",
            "localized_title": "Perfect Blue",
            "max_progress": 1,
            "image": "https://example.com/perfect-blue.jpg",
            "synopsis": "",
            "genres": [],
            "score": 0.0,
            "score_count": 0,
            "details": {
                "format": None,
                "start_date": None,
                "end_date": None,
                "status": None,
                "episodes": 1,
                "runtime": None,
                "studios": [],
                "season": None,
                "broadcast": None,
                "source": None,
            },
            "related": {
                "related_anime": [],
                "recommendations": [],
            },
        }
        payload = {
            "Event": "Stop",
            "Item": {
                "Name": "Perfect Blue",
                "ProductionYear": 1997,
                "Type": "Movie",
                "ProviderIds": {"Imdb": "tt0156887"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify movie was created and marked as completed
        movie = Anime.objects.get(
            item__media_id="437",
            user=self.user,
        )
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_anime_episode_mark_played(self, mock_tv_with_seasons, mock_find, mock_mal_anime):
        """Test webhook handles anime episode mark played event."""
        self.user.anime_enabled = True
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = False
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save()

        mock_find.return_value = {
            "tv_episode_results": [{"show_id": 52991, "season_number": 1, "episode_number": 1}],
            "tv_results": [],
        }
        mock_tv_with_seasons.return_value = FRIEREN_TV_DETAILS_RETURN_VALUE
        mock_mal_anime.return_value = FRIEREN_MAL_ANIME_RETURN_VALUE

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The Journey's End",
                "ProviderIds": {
                    "Tvdb": "9350138",
                    "Imdb": "tt23861604",
                },
                "UserData": {"Played": True},
                "SeriesName": "Frieren: Beyond Journey's End",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify anime was created and marked as in progress
        anime = Anime.objects.get(
            item__media_id="52991",
            user=self.user,
        )
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertEqual(anime.progress, 1)

    @patch("integrations.webhooks.base.anime_mapping.load_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anime_episode_prefers_tmdb_mapping_for_later_season(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_load_mapping_data,
    ):
        """TMDB grouped-anime mappings should win when TVDB mapping disagrees."""
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 12345,
                    "season_number": 2,
                    "episode_number": 11,
                },
            ],
            "tv_results": [],
        }
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "source": "tmdb",
            "source_url": "https://www.themoviedb.org/tv/12345",
            "media_type": "tv",
            "title": "Hell's Paradise",
            "original_title": "Hell's Paradise",
            "localized_title": "Hell's Paradise",
            "max_progress": 13,
            "image": "https://example.com/hells-paradise.jpg",
            "synopsis": "",
            "genres": ["Animation"],
            "score": 0.0,
            "score_count": 0,
            "details": {
                "format": "TV",
                "first_air_date": None,
                "last_air_date": None,
                "status": None,
                "seasons": None,
                "episodes": 13,
                "runtime": None,
                "studios": [],
                "country": [],
                "languages": [],
            },
            "cast": [],
            "crew": [],
            "studios_full": [],
            "related": {
                "seasons": [],
                "recommendations": [],
            },
            "tvdb_id": "402474",
            "external_links": {},
            "last_episode_season": None,
            "next_episode_season": None,
            "providers": {},
        }
        mock_load_mapping_data.return_value = {
            "hells-paradise-tvdb": {
                "tvdb_id": "402474",
                "tvdb_season": 2,
                "tvdb_epoffset": -13,
                "mal_id": "46569",
            },
            "hells-paradise-tmdb": {
                "tmdb_show_id": "12345",
                "tmdb_season": 2,
                "tmdb_epoffset": 0,
                "mal_id": "60067",
            },
        }

        def mal_side_effect(media_id):
            if str(media_id) == "60067":
                return {
                    "media_id": "60067",
                    "source": "mal",
                    "source_url": "https://myanimelist.net/anime/60067",
                    "media_type": "anime",
                    "title": "Hell's Paradise 2nd Season",
                    "original_title": "Hell's Paradise 2nd Season",
                    "localized_title": "Hell's Paradise 2nd Season",
                    "max_progress": 12,
                    "image": "https://example.com/hells-paradise-s2.jpg",
                    "synopsis": "",
                    "genres": [],
                    "score": 0.0,
                    "score_count": 0,
                    "details": {
                        "format": None,
                        "start_date": None,
                        "end_date": None,
                        "status": None,
                        "episodes": 12,
                        "runtime": None,
                        "studios": [],
                        "season": None,
                        "broadcast": None,
                        "source": None,
                    },
                    "related": {
                        "related_anime": [],
                        "recommendations": [],
                    },
                }
            if str(media_id) == "46569":
                return {
                    "media_id": "46569",
                    "source": "mal",
                    "source_url": "https://myanimelist.net/anime/46569",
                    "media_type": "anime",
                    "title": "Hell's Paradise",
                    "original_title": "Hell's Paradise",
                    "localized_title": "Hell's Paradise",
                    "max_progress": 13,
                    "image": "https://example.com/hells-paradise-s1.jpg",
                    "synopsis": "",
                    "genres": [],
                    "score": 0.0,
                    "score_count": 0,
                    "details": {
                        "format": None,
                        "start_date": None,
                        "end_date": None,
                        "status": None,
                        "episodes": 13,
                        "runtime": None,
                        "studios": [],
                        "season": None,
                        "broadcast": None,
                        "source": None,
                    },
                    "related": {
                        "related_anime": [],
                        "recommendations": [],
                    },
                }
            msg = f"Unexpected MAL ID requested: {media_id}"
            raise AssertionError(msg)

        mock_mal_anime.side_effect = mal_side_effect

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "Episode 11",
                "ProviderIds": {
                    "Tmdb": "12345",
                    "Tvdb": "402474",
                },
                "UserData": {"Played": True},
                "SeriesName": "Hell's Paradise",
                "ParentIndexNumber": 2,
                "IndexNumber": 11,
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        anime = Anime.objects.get(item__media_id="60067", user=self.user)
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertEqual(anime.progress, 11)
        self.assertFalse(
            Anime.objects.filter(item__media_id="46569", user=self.user).exists(),
        )

    @patch("integrations.webhooks.base.BaseWebhookProcessor._handle_tv_episode")
    @patch("integrations.webhooks.base.anime_mapping.load_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anime_episode_falls_back_to_tv_when_mapping_progress_is_impossible(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_load_mapping_data,
        mock_handle_tv_episode,
    ):
        """Impossible anime progress should not create a bogus flat anime entry."""
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 12345,
                    "season_number": 2,
                    "episode_number": 11,
                },
            ],
            "tv_results": [],
        }
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "title": "Hell's Paradise",
            "image": "https://example.com/hells-paradise.jpg",
            "tvdb_id": "402474",
            "season/2": {"episodes": [{"episode_number": 11}]},
        }
        mock_load_mapping_data.return_value = {
            "hells-paradise-tvdb": {
                "tvdb_id": "402474",
                "tvdb_season": 2,
                "tvdb_epoffset": -13,
                "mal_id": "46569",
            },
        }
        mock_mal_anime.return_value = {
            "media_id": "46569",
            "title": "Hell's Paradise",
            "image": "https://example.com/hells-paradise-s1.jpg",
            "max_progress": 13,
        }

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "Episode 11",
                "ProviderIds": {
                    "Tvdb": "402474",
                },
                "UserData": {"Played": True},
                "SeriesName": "Hell's Paradise",
                "ParentIndexNumber": 2,
                "IndexNumber": 11,
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Anime.objects.count(), 0)
        mock_handle_tv_episode.assert_called_once()
        self.assertEqual(
            mock_handle_tv_episode.call_args.args[:3],
            (12345, 2, 11),
        )

    def test_ignored_event_types(self):
        """Test webhook ignores irrelevant event types."""
        payload = {
            "Event": "SomeOtherEvent",
            "Item": {
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "12345"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Movie.objects.count(), 0)

    def test_missing_tmdb_id(self):
        """Test webhook handles missing TMDB ID gracefully."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "ProviderIds": {},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Movie.objects.count(), 0)

    def test_mark_unplayed(self):
        """Test webhook handles not finished events."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": False},
            },
        }
        self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603")
        self.assertEqual(movie.progress, 0)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)

    def test_repeated_watch(self):
        """Test webhook handles repeated watches."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "ProductionYear": 1999,
                "Name": "The Matrix",
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": True},
            },
        }

        # First watch
        self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Second watch
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.filter(item__media_id="603")
        self.assertEqual(movie.count(), 2)
        self.assertEqual(movie[0].status, Status.COMPLETED.value)
        self.assertEqual(movie[1].status, Status.COMPLETED.value)

    def test_extract_external_ids(self):
        """Test extracting external IDs from provider payload."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "ProviderIds": {
                    "Tmdb": "603",
                    "Tvdb": "169",
                },
            },
        }

        expected = {
            "tmdb_id": "603",
            "imdb_id": None,
            "tvdb_id": "169",
        }

        result = JellyfinWebhookProcessor()._extract_external_ids(payload)
        if result != expected:
            msg = f"Expected {expected}, got {result}"
            raise AssertionError(msg)

    def test_extract_external_ids_empty(self):
        """Test handling empty provider payload."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "ProviderIds": {},
            },
        }

        expected = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
        }

        result = JellyfinWebhookProcessor()._extract_external_ids(payload)
        if result != expected:
            msg = f"Expected {expected}, got {result}"
            raise AssertionError(msg)

    def test_extract_external_ids_missing(self):
        """Test handling missing ProviderIds."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
            },
        }
        expected = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
        }

        result = JellyfinWebhookProcessor()._extract_external_ids(payload)
        if result != expected:
            msg = f"Expected {expected}, got {result}"
            raise AssertionError(msg)

    @patch("app.providers.tmdb.find")
    def test_play_event_stores_live_playback_state(self, mock_find):
        """Play events should create live playback state for the home card."""
        mock_find.return_value = TMDB_FIND_RETURN_VALUE["friends_s1e1"]

        payload = {
            "Event": "Play",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-1",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "RunTimeTicks": 26660000000,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": False},
            },
            "PlaybackPositionTicks": 14470000000,
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(state)
        self.assertEqual(state["media_type"], MediaTypes.EPISODE.value)
        self.assertEqual(state["media_id"], str(TMDB_TV_IDS["friends"]))
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_PLAYING)
        self.assertEqual(state["season_number"], 1)
        self.assertEqual(state["episode_number"], 1)
        self.assertEqual(state["duration_seconds"], 2666)
        self.assertEqual(state["view_offset_seconds"], 1447)

    @patch("app.providers.tmdb.find")
    def test_pause_and_stop_events_update_live_playback_state(self, mock_find):
        """Pause should keep card state; stop should transition to stopped."""
        mock_find.return_value = TMDB_FIND_RETURN_VALUE["friends_s1e1"]

        play_payload = {
            "Event": "Play",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-2",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "RunTimeTicks": 26660000000,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": False},
            },
            "PlaybackPositionTicks": 6000000000,
        }
        pause_payload = {
            "Event": "Pause",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-2",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "RunTimeTicks": 26660000000,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": False},
            },
            "PlaybackPositionTicks": 7210000000,
        }
        stop_payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-2",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": True},
            },
        }

        # Play
        play_response = self.client.post(
            self.url,
            data=json.dumps(play_payload),
            content_type="application/json",
        )
        self.assertEqual(play_response.status_code, 200)

        # Pause
        pause_response = self.client.post(
            self.url,
            data=json.dumps(pause_payload),
            content_type="application/json",
        )
        self.assertEqual(pause_response.status_code, 200)

        paused_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(paused_state)
        self.assertEqual(
            paused_state["status"], live_playback.PLAYBACK_STATUS_PAUSED,
        )
        self.assertEqual(paused_state["view_offset_seconds"], 721)

        # Stop
        stop_response = self.client.post(
            self.url,
            data=json.dumps(stop_payload),
            content_type="application/json",
        )
        self.assertEqual(stop_response.status_code, 200)

        stopped_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(stopped_state)
        self.assertEqual(
            stopped_state["status"],
            live_playback.PLAYBACK_STATUS_STOPPED,
        )



    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_jellyfin_provider_priority_disabled_uses_tmdb_default(self, mock_tv_with_seasons, mock_find):
        """When provider priority setting is disabled, webhooks should use default TMDB tracking."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = False
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        # https://developer.themoviedb.org/reference/find-by-id
        mock_find.return_value = TMDB_FIND_RETURN_VALUE["friends_s1e1"]
        # https://developer.themoviedb.org/reference/tv-series-details - long reply, simplified
        mock_tv_with_seasons.return_value = TMDB_TV_DETAILS_RETURN_VALUE["friends"]

        payload: dict = JELLYFIN_STOP_PAYLOADS["friends_s1e1_played"]

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id=str(TMDB_TV_IDS["friends"]))
        self.assertEqual(tv_item.source, Sources.TMDB.value)

    def test_jellyfin_tracks_tv_under_tvdb_when_preferred_provider_enabled(self):
        """When TVDB is user's preferred provider, webhooks should track under TVDB."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = False
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        payload = JELLYFIN_STOP_PAYLOADS["friends_s1e1_played"]

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id=str(TVDB_TV_IDS["friends"]))
        self.assertEqual(tv_item.source, Sources.TVDB.value)

    @patch("app.providers.tmdb.find")
    @patch("app.providers.mal.anime")
    def test_jellyfin_tracks_anime_under_mal_when_preferred_provider_enabled(self, mock_mal_anime, mock_find):
        """When MAL is user's preferred provider, anime webhooks should track under MAL."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = False
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save()

        mock_find.return_value = {
            "tv_episode_results": [{"show_id": 209867, "season_number": 1, "episode_number": 1}],
            "tv_results": [],
        }
        mock_mal_anime.return_value = {
            "media_id": "52991",
            "source": "mal",
            "source_url": "https://myanimelist.net/anime/52991",
            "media_type": "anime",
            "title": "Frieren: Beyond Journey's End",
            "original_title": "Frieren: Beyond Journey's End",
            "localized_title": "Frieren: Beyond Journey's End",
            "max_progress": 28,
            "image": "https://example.com/frieren.jpg",
            "synopsis": "",
            "genres": [],
            "score": 0.0,
            "score_count": 0,
            "details": {
                "format": None,
                "start_date": None,
                "end_date": None,
                "status": None,
                "episodes": 28,
                "runtime": None,
                "studios": [],
                "season": None,
                "broadcast": None,
                "source": None,
            },
            "related": {
                "related_anime": [],
                "recommendations": [],
            },
        }

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The Journey's End",
                "ProviderIds": {"Tvdb": "9350138", "Imdb": "tt23861604"},
                "UserData": {"Played": True},
                "SeriesName": "Frieren: Beyond Journey's End",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            },
        }

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        anime = Anime.objects.get(item__media_id="52991", user=self.user)
        self.assertEqual(anime.item.source, Sources.MAL.value)

    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_jellyfin_fallback_to_tmdb_when_preferred_provider_id_not_in_payload(self, mock_tv_with_seasons, mock_find):
        """When preferred provider ID is not in payload, should fall back to TMDB."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = False
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        mock_find.return_value = TMDB_FIND_RETURN_VALUE["friends_s1e1"]
        mock_tv_with_seasons.return_value = TMDB_TV_DETAILS_RETURN_VALUE["friends"]

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "ProviderIds": {"Tmdb": "1668", "Imdb": "tt0583459"},
                "UserData": {"Played": True},
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            },
        }

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id="1668")
        self.assertEqual(tv_item.source, Sources.TMDB.value)

    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_jellyfin_movie_uses_tmdb_default_regardless_of_setting(self, mock_tv_with_seasons, mock_find):
        """Movies should always use TMDB regardless of provider priority setting."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = False
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        mock_find.return_value = {"movie_results": [{"id": 603}]}
        mock_tv_with_seasons.return_value = {
            "media_id": "603",
            "title": "The Matrix",
            "image": "https://example.com/matrix.jpg",
            "release_date": "1999-03-30",
        }

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "ProviderIds": {"Tmdb": "603", "Tvdb": "169"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.item.source, Sources.TMDB.value)

    def test_jellyfin_get_preferred_source_returns_none_when_disabled(self):
        """_get_jellyfin_preferred_source returns None when feature is disabled."""
        processor = JellyfinWebhookProcessor()
        result = processor._get_jellyfin_preferred_source(self.user, MediaTypes.TV.value)
        self.assertIsNone(result)

    def test_jellyfin_get_preferred_source_returns_user_default_for_tv(self):
        """_get_jellyfin_preferred_source returns user's tv_metadata_source_default."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        processor = JellyfinWebhookProcessor()
        result = processor._get_jellyfin_preferred_source(self.user, MediaTypes.TV.value)
        self.assertEqual(result, Sources.TVDB.value)

    def test_jellyfin_get_preferred_source_returns_user_default_for_anime(self):
        """_get_jellyfin_preferred_source returns user's anime_metadata_source_default."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.anime_metadata_source_default = Sources.MAL.value
        self.user.save()

        processor = JellyfinWebhookProcessor()
        result = processor._get_jellyfin_preferred_source(self.user, MediaTypes.ANIME.value)
        self.assertEqual(result, Sources.MAL.value)

    def test_jellyfin_get_preferred_source_returns_none_for_movies(self):
        """_get_jellyfin_preferred_source returns None for movies (always TMDB)."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        processor = JellyfinWebhookProcessor()
        result = processor._get_jellyfin_preferred_source(self.user, MediaTypes.MOVIE.value)
        self.assertIsNone(result)

    def test_jellyfin_resolve_media_id_to_preferred_source_returns_tuple_when_match_found(self):
        """_resolve_media_id_to_preferred_source returns (id, source, season, episode) when match found."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        processor = JellyfinWebhookProcessor()
        ids = {"tmdb_id": "1668", "tvdb_id": str(TVDB_TV_IDS["friends"])}

        result = processor._resolve_media_id_to_preferred_source(
            self.user, MediaTypes.TV.value, ids, season_number=1, episode_number=1
        )

        self.assertEqual(result, (ids["tvdb_id"], Sources.TVDB.value, 1, 1))

    def test_jellyfin_resolve_media_id_to_preferred_source_returns_none_when_no_match(self):
        """_resolve_media_id_to_preferred_source returns None when preferred provider ID not in payload."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.tv_metadata_source_default = Sources.TVDB.value
        self.user.save()

        processor = JellyfinWebhookProcessor()
        ids = {"tmdb_id": str(TMDB_TV_IDS["friends"])}

        result = processor._resolve_media_id_to_preferred_source(
            self.user, MediaTypes.TV.value, ids, season_number=1, episode_number=1
        )

        self.assertEqual(result, (None, None, None, None))



    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_jellyfin_match_existing_disabled_uses_normal_flow(self, mock_tv_with_seasons, mock_find):
        """When match existing setting is disabled, should use normal TMDB-first flow."""
        self.assertFalse(self.user.jellyfin_match_existing_enabled)
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = False
        self.user.save()

        mock_find.return_value = TMDB_FIND_RETURN_VALUE["friends_s1e1"]
        mock_tv_with_seasons.return_value = TMDB_TV_DETAILS_RETURN_VALUE["friends"]

        payload = JELLYFIN_STOP_PAYLOADS["friends_s1e1_played"]

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id=str(TMDB_TV_IDS["friends"]))
        self.assertEqual(tv_item.source, Sources.TMDB.value)

    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_jellyfin_updates_existing_mal_entry_when_match_enabled(self, mock_tv_with_seasons, mock_find):
        """If user has show tracked under MAL, updates should go to MAL entry."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        mal_item = Item.objects.create(
            media_id="4501",
            source=Sources.MAL.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
        )
        TV.objects.create(item=mal_item, user=self.user, status=Status.IN_PROGRESS.value)

        ItemProviderLink.objects.create(
            item=mal_item,
            provider=Sources.TMDB.value,
            provider_media_id="1396",
            provider_media_type=MediaTypes.TV.value,
        )

        mock_find.return_value = BREAKING_BAD_TMDB_FIND_RETURN_VALUE
        mock_tv_with_seasons.return_value = BREAKING_BAD_TV_DETAILS_RETURN_VALUE

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "Pilot",
                "ProviderIds": {"Tmdb": "1396"},
                "UserData": {"Played": True},
                "SeriesName": "Breaking Bad",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            },
        }

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        # The TV show Item + a Season Item are created
        self.assertEqual(Item.objects.filter(media_type=MediaTypes.TV.value).count(), 1)
        self.assertEqual(Item.objects.get(media_type=MediaTypes.TV.value).source, Sources.MAL.value)

        tv = TV.objects.get(item__media_id="4501", user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_jellyfin_updates_existing_tvdb_entry_when_match_enabled(self, mock_tv_with_seasons, mock_find):
        """If user has show tracked under TVDB, updates should go to TVDB entry."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        tvdb_item = Item.objects.create(
            media_id="81189",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
        )
        TV.objects.create(item=tvdb_item, user=self.user, status=Status.IN_PROGRESS.value)

        # Link the TVDB item to TMDB so Feature #2 can find it via TMDB ID
        ItemProviderLink.objects.create(
            item=tvdb_item,
            provider=Sources.TMDB.value,
            provider_media_id="1396",
            provider_media_type=MediaTypes.TV.value,
        )

        mock_find.return_value = BREAKING_BAD_TMDB_FIND_RETURN_VALUE
        mock_tv_with_seasons.return_value = BREAKING_BAD_TV_DETAILS_RETURN_VALUE

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "Pilot",
                "ProviderIds": {"Tmdb": "1396"},
                "UserData": {"Played": True},
                "SeriesName": "Breaking Bad",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            },
        }

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        # The TV show Item + a Season Item are created
        self.assertEqual(Item.objects.filter(media_type=MediaTypes.TV.value).count(), 1)
        self.assertEqual(Item.objects.get(media_type=MediaTypes.TV.value).source, Sources.TVDB.value)

    @patch("app.providers.tmdb.find")
    def test_jellyfin_updates_existing_movie_by_tmdb_id(self, mock_find):
        """If user has movie tracked, updates should go to existing movie entry."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        tmdb_item = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
        )
        Movie.objects.create(item=tmdb_item, user=self.user, status=Status.COMPLETED.value)

        mock_find.return_value = MATRIX_TMDB_FIND_RETURN_VALUE

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(Movie.objects.count(), 1)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)

    def test_jellyfin_find_existing_item_returns_none_when_disabled(self):
        """_find_existing_item returns None when feature is disabled."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = False
        self.user.save()

        processor = JellyfinWebhookProcessor()

        result, created = processor._find_existing_item(
            self.user, MediaTypes.TV.value, {"tmdb_id": "1668"},
        )

        self.assertIsNone(result)
        self.assertTrue(created)

    def test_jellyfin_find_existing_item_returns_none_when_no_tracking_instance(self):
        """_find_existing_item returns None when user has no tracking instance."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
        )

        processor = JellyfinWebhookProcessor()

        result, created = processor._find_existing_item(
            self.user, MediaTypes.TV.value, {"tmdb_id": "1668"},
        )

        self.assertIsNone(result)
        self.assertTrue(created)

    def test_jellyfin_find_existing_item_returns_item_when_match_found(self):
        """_find_existing_item returns item when match found."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        tmdb_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
        )
        TV.objects.create(item=tmdb_item, user=self.user, status=Status.IN_PROGRESS.value)

        processor = JellyfinWebhookProcessor()

        result, created = processor._find_existing_item(
            self.user, MediaTypes.TV.value, {"tmdb_id": "1668"}
        )

        self.assertEqual(result, tmdb_item)
        self.assertFalse(created)

    def test_jellyfin_find_existing_item_by_provider_link(self):
        """_find_existing_item finds item via ItemProviderLink."""
        self.user.jellyfin_provider_priority_enabled = False
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        mal_item = Item.objects.create(
            media_id="4501",
            source=Sources.MAL.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
        )
        TV.objects.create(item=mal_item, user=self.user, status=Status.IN_PROGRESS.value)

        ItemProviderLink.objects.create(
            item=mal_item,
            provider=Sources.TMDB.value,
            provider_media_id="1396",
            provider_media_type=MediaTypes.TV.value,
        )

        processor = JellyfinWebhookProcessor()

        result, created = processor._find_existing_item(
            self.user, MediaTypes.TV.value, {"tmdb_id": "1396"}
        )

        self.assertEqual(result, mal_item)
        self.assertFalse(created)

    def test_update_jellyfin_settings_endpoint(self):
        """Test the update_jellyfin_settings POST endpoint."""
        self.client.login(username=self.TEST_USERNAME, password=self.TEST_PASSWORD)

        url = reverse("update_jellyfin_settings")

        self.assertFalse(self.user.jellyfin_provider_priority_enabled)
        self.assertFalse(self.user.jellyfin_match_existing_enabled)

        response = self.client.post(url, {
            "jellyfin_provider_priority_enabled": "on",
            "jellyfin_match_existing_enabled": "on",
        })

        self.assertEqual(response.status_code, 302)

        self.user.refresh_from_db()
        self.assertTrue(self.user.jellyfin_provider_priority_enabled)
        self.assertTrue(self.user.jellyfin_match_existing_enabled)

    def test_update_jellyfin_settings_disable_features(self):
        """Test disabling Jellyfin features."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        self.client.login(username=self.TEST_USERNAME, password=self.TEST_PASSWORD)

        url = reverse("update_jellyfin_settings")

        response = self.client.post(url, {
            "jellyfin_provider_priority_enabled": "",
            "jellyfin_match_existing_enabled": "",
        })

        self.assertEqual(response.status_code, 302)

        self.user.refresh_from_db()
        self.assertFalse(self.user.jellyfin_provider_priority_enabled)
        self.assertFalse(self.user.jellyfin_match_existing_enabled)

    def test_update_jellyfin_settings_partial_update(self):
        """Test updating only one feature."""
        self.user.jellyfin_provider_priority_enabled = True
        self.user.jellyfin_match_existing_enabled = True
        self.user.save()

        self.client.login(username=self.TEST_USERNAME, password=self.TEST_PASSWORD)

        url = reverse("update_jellyfin_settings")

        response = self.client.post(url, {
            "jellyfin_provider_priority_enabled": "on",
            "jellyfin_match_existing_enabled": "",
        })

        self.assertEqual(response.status_code, 302)

        self.user.refresh_from_db()
        self.assertTrue(self.user.jellyfin_provider_priority_enabled)
        self.assertFalse(self.user.jellyfin_match_existing_enabled)
