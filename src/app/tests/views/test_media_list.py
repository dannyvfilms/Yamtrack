import json
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import cache_utils
from app.models import (
    Album,
    Anime,
    Artist,
    ArtistTracker,
    Book,
    Comic,
    ComicIssue,
    CollectionEntry,
    Episode,
    Game,
    Item,
    Manga,
    MediaTypes,
    Music,
    Movie,
    Season,
    Sources,
    Status,
    TV,
)
from app.templatetags import app_tags
from events.models import Event


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        Item.objects.bulk_create(
            [
                Item(
                    media_id=movies_id[i - 1],
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"Test Movie {i}",
                    image="http://example.com/image.jpg",
                )
                for i in range(1, 6)
            ],
        )
        created_items = {
            item.media_id: item
            for item in Item.objects.filter(
                media_id__in=movies_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            )
        }

        Movie.objects.bulk_create(
            [
                Movie(
                    item=created_items[movies_id[i - 1]],
                    user=self.user,
                    status=(
                        Status.COMPLETED.value
                        if i < num_completed
                        else Status.IN_PROGRESS.value
                    ),
                    progress=1 if i < num_completed else 0,
                    score=i,
                )
                for i in range(1, 6)
            ],
        )

    def _create_game_entry(
        self,
        media_id,
        title,
        *,
        hltb_minutes=None,
        igdb_seconds=None,
    ):
        provider_game_lengths = {}
        provider_game_lengths_source = ""
        provider_game_lengths_match = ""

        if hltb_minutes:
            provider_game_lengths = {
                "active_source": "hltb",
                "hltb": {
                    "game_id": int(media_id) if str(media_id).isdigit() else 0,
                    "summary": {
                        "all_styles_minutes": hltb_minutes,
                    },
                    "counts": {
                        "all_styles": 12,
                    },
                },
            }
            provider_game_lengths_source = "hltb"
            provider_game_lengths_match = "exact_title_year"
        elif igdb_seconds:
            provider_game_lengths = {
                "active_source": "igdb",
                "igdb": {
                    "game_id": int(media_id) if str(media_id).isdigit() else 0,
                    "summary": {
                        "normally_seconds": igdb_seconds,
                        "count": 4,
                    },
                    "raw": [],
                },
            }
            provider_game_lengths_source = "igdb"
            provider_game_lengths_match = "igdb_fallback"

        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title=title,
            image="http://example.com/game.jpg",
            provider_game_lengths=provider_game_lengths,
            provider_game_lengths_source=provider_game_lengths_source,
            provider_game_lengths_match=provider_game_lengths_match,
        )
        Game.objects.bulk_create(
            [
                Game(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=60,
                ),
            ],
        )
        return item

    def _create_movie_runtime_entry(self, media_id, title, runtime_minutes=None):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image="http://example.com/movie-runtime.jpg",
            runtime_minutes=runtime_minutes,
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                ),
            ],
        )
        return item

    def _create_movie_popularity_entry(self, media_id, title, rank):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image="http://example.com/movie-popularity.jpg",
            trakt_rating=8.0,
            trakt_rating_count=1000,
            trakt_popularity_score=1000.0 / rank,
            trakt_popularity_rank=rank,
            trakt_popularity_fetched_at=timezone.now() - timedelta(days=1),
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                ),
            ],
        )
        return item

    def _create_movie_critic_rating_entry(self, media_id, title, provider_rating=None):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image="http://example.com/movie-critic-rating.jpg",
            provider_rating=provider_rating,
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                ),
            ],
        )
        return item

    def _create_tv_runtime_entry(self, media_id, title, episode_runtimes, *, progress=0):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="http://example.com/tv-runtime.jpg",
        )
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title=f"{title} Season 1",
            image="http://example.com/tv-season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        released_at = timezone.now() - timedelta(days=30)
        for episode_number, runtime_minutes in enumerate(episode_runtimes, start=1):
            episode_item = Item.objects.create(
                media_id=str(media_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"{title} Episode {episode_number}",
                image="http://example.com/tv-episode.jpg",
                season_number=1,
                episode_number=episode_number,
                runtime_minutes=runtime_minutes,
                release_datetime=released_at + timedelta(days=episode_number),
            )
            if episode_number <= progress:
                Episode.objects.create(
                    item=episode_item,
                    related_season=season,
                    end_date=timezone.now() - timedelta(days=episode_number),
                )

        return item

    def _create_tv_seasonal_entry(self, media_id, title, season_configs):
        """Create a TV show with explicit per-season status and release counts."""
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="http://example.com/tv-seasonal.jpg",
        )
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        now = timezone.now()
        for season_config in season_configs:
            season_number = season_config["season_number"]
            season_item = Item.objects.create(
                media_id=str(media_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=f"{title} Season {season_number}",
                image="http://example.com/tv-seasonal-season.jpg",
                season_number=season_number,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=tv,
                status=season_config["status"],
            )

            released_episodes = season_config["released_episodes"]
            watched_episodes = season_config.get("watched_episodes", 0)

            for episode_number in range(1, released_episodes + 1):
                episode_item = Item.objects.create(
                    media_id=str(media_id),
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    title=f"{title} S{season_number:02d}E{episode_number:02d}",
                    image="http://example.com/tv-seasonal-episode.jpg",
                    season_number=season_number,
                    episode_number=episode_number,
                    release_datetime=now - timedelta(days=episode_number),
                )
                if episode_number <= watched_episodes:
                    Episode.objects.create(
                        item=episode_item,
                        related_season=season,
                        end_date=now - timedelta(days=episode_number),
                    )

        return tv

    def _create_anime_runtime_entry(
        self,
        media_id,
        title,
        *,
        runtime_minutes,
        episode_count,
        progress=0,
    ):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title=title,
            image="http://example.com/anime-runtime.jpg",
            runtime_minutes=runtime_minutes,
        )
        Anime.objects.bulk_create(
            [
                Anime(
                    item=item,
                    user=self.user,
                    status=Status.PLANNING.value,
                    progress=progress,
                ),
            ],
        )
        Event.objects.create(
            item=item,
            content_number=episode_count,
            datetime=timezone.now() - timedelta(days=1),
        )
        return item

    def _create_tv_next_episode_air_date_entry(
        self,
        media_id,
        title,
        episode_release_datetimes,
        *,
        progress=0,
        library_media_type=None,
    ):
        item_kwargs = {
            "media_id": str(media_id),
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "title": title,
            "image": "http://example.com/tv-next-episode.jpg",
        }
        if library_media_type is not None:
            item_kwargs["library_media_type"] = library_media_type

        item = Item.objects.create(**item_kwargs)
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item_kwargs = {
            "media_id": str(media_id),
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "title": f"{title} Season 1",
            "image": "http://example.com/tv-next-episode-season.jpg",
            "season_number": 1,
        }
        if library_media_type is not None:
            season_item_kwargs["library_media_type"] = library_media_type

        season_item = Item.objects.create(**season_item_kwargs)
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        watched_at = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        for episode_number, release_datetime in enumerate(episode_release_datetimes, start=1):
            event_datetime = release_datetime
            if event_datetime is None:
                event_datetime = timezone.datetime.min.replace(
                    tzinfo=timezone.get_current_timezone(),
                )

            Event.objects.create(
                item=season_item,
                content_number=episode_number,
                datetime=event_datetime,
                notification_sent=False,
            )

            episode_item_kwargs = {
                "media_id": str(media_id),
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "title": f"{title} Episode {episode_number}",
                "image": "http://example.com/tv-next-episode-episode.jpg",
                "season_number": 1,
                "episode_number": episode_number,
            }
            if library_media_type is not None:
                episode_item_kwargs["library_media_type"] = library_media_type
            if release_datetime is not None:
                episode_item_kwargs["release_datetime"] = release_datetime

            episode_item = Item.objects.create(**episode_item_kwargs)
            if episode_number <= progress:
                Episode.objects.create(
                    item=episode_item,
                    related_season=season,
                    end_date=watched_at - timedelta(days=episode_number),
                )

        return tv, season

    def _create_anime_next_episode_air_date_entry(
        self,
        media_id,
        title,
        episode_air_dates,
        *,
        progress=0,
        status=Status.IN_PROGRESS.value,
    ):
        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title=title,
            image="http://example.com/anime-next-episode.jpg",
        )
        Anime.objects.create(
            item=item,
            user=self.user,
            status=status,
            progress=progress,
        )

        for episode_number, air_datetime in episode_air_dates:
            Event.objects.create(
                item=item,
                content_number=episode_number,
                datetime=air_datetime,
                notification_sent=False,
            )

        return item

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_music_media_list_uses_canonical_artist_links(self):
        """Music list should render artist cells with canonical shared-detail URLs."""
        artist = Artist.objects.create(name="List Artist")
        album = Album.objects.create(title="List Album", artist=artist)
        item = Item.objects.create(
            media_id="list-track-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="List Track",
            image="http://example.com/list-track.jpg",
        )
        media = Music.objects.create(
            item=item,
            user=self.user,
            artist=artist,
            album=album,
            status=Status.COMPLETED.value,
            progress=1,
        )

        response = self.client.get(reverse("medialist", args=[MediaTypes.MUSIC.value]))

        self.assertEqual(response.status_code, 200)
        rendered_cell = render_to_string(
            "app/components/cells/artist_name_cell.html",
            {"media": media},
        )
        self.assertIn(
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "list-artist",
                },
            ),
            rendered_cell,
        )

    def test_music_media_list_keeps_artist_genres_as_primary_filter_data(self):
        artist = Artist.objects.create(
            name="Genre Artist",
            genres=["Art Rock"],
            country="gb",
        )
        ArtistTracker.objects.create(
            user=self.user,
            artist=artist,
            status=Status.COMPLETED.value,
        )

        response = self.client.get(reverse("medialist", args=[MediaTypes.MUSIC.value]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filter_data"]["genres"], ["Art Rock"])
        self.assertNotIn("implied_genres", response.context["filter_data"])

    def test_movie_grid_aggregates_duplicate_completed_plays(self):
        """Grid cards should show total plays across duplicate completed movie entries."""
        item = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        existing_play = Movie.objects.get(item=item, user=self.user)
        existing_play.score = 9
        existing_play.save(update_fields=["score"])

        second_date = timezone.now() - timedelta(days=7)
        third_date = timezone.now() - timedelta(days=1)
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    score=None,
                    start_date=second_date,
                    end_date=second_date,
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    score=9,
                    start_date=third_date,
                    end_date=third_date,
                ),
            ],
        )

        latest = Movie.objects.filter(item=item, user=self.user).order_by("-id").first()
        latest.score = 10
        latest.save(update_fields=["score"])

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Test+Movie+1&sort=title&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "Test Movie 1")
        self.assertContains(response, "3 plays")

    def test_movie_grid_counts_completed_plays_when_progress_is_zero(self):
        """Completed movie duplicates should count as plays even when progress is zero."""
        item = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )

        first_play = Movie.objects.get(item=item, user=self.user)
        first_play.status = Status.COMPLETED.value
        first_play.progress = 1
        first_play.end_date = timezone.now() - timedelta(days=220)
        first_play.save()

        second_date = timezone.now() - timedelta(days=126)
        third_date = timezone.now() - timedelta(days=90)
        fourth_date = timezone.now() - timedelta(days=9)
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=second_date,
                ),
                # Simulate legacy/completed rows where progress was never normalized to 1.
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=0,
                    end_date=third_date,
                    score=9,
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=0,
                    end_date=fourth_date,
                    score=10,
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Test+Movie+1&sort=title&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "Test Movie 1")
        self.assertContains(response, "4 plays")

    def test_supported_media_sort_shows_plays_option(self):
        """Movies, TV, and Anime should expose the plays sort option."""
        movie_response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))
        tv_response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))
        anime_response = self.client.get(reverse("medialist", args=[MediaTypes.ANIME.value]))

        self.assertContains(movie_response, "toggleSort('plays')")
        self.assertContains(tv_response, "toggleSort('plays')")
        self.assertContains(anime_response, "toggleSort('plays')")
        self.assertContains(movie_response, "toggleSort('release_date')")
        self.assertContains(movie_response, "toggleSort('date_added')")

    def test_non_supported_media_sort_hides_plays_option_and_falls_back(self):
        """Non movie/tv/anime media types should hide plays sort and fallback to title."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?sort=plays",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('plays')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.book_sort, "title")

    def test_supported_media_sort_shows_time_watched_option(self):
        """Movies, TV, and Anime should expose the time watched sort option."""
        movie_response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))
        tv_response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))
        anime_response = self.client.get(reverse("medialist", args=[MediaTypes.ANIME.value]))

        self.assertContains(movie_response, "toggleSort('time_watched')")
        self.assertContains(tv_response, "toggleSort('time_watched')")
        self.assertContains(anime_response, "toggleSort('time_watched')")

    def test_non_supported_media_sort_hides_time_watched_option_and_falls_back(self):
        """Non movie/tv/anime media types should hide time watched sort and fallback."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?sort=time_watched",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('time_watched')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.book_sort, "title")

    def test_supported_media_sort_shows_next_episode_air_date_option(self):
        """TV, Season, and Anime should expose the next episode air date sort option."""
        tv_response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))
        season_response = self.client.get(reverse("medialist", args=[MediaTypes.SEASON.value]))
        anime_response = self.client.get(reverse("medialist", args=[MediaTypes.ANIME.value]))

        self.assertContains(tv_response, "toggleSort('next_episode_air_date')")
        self.assertContains(season_response, "toggleSort('next_episode_air_date')")
        self.assertContains(anime_response, "toggleSort('next_episode_air_date')")

    def test_non_supported_media_sort_hides_next_episode_air_date_option_and_falls_back(self):
        """Non show-like media types should hide next-episode air-date sort and fallback."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?sort=next_episode_air_date",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('next_episode_air_date')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.book_sort, "title")

    def test_game_sort_dropdown_includes_time_to_beat_option(self):
        self._create_game_entry("325609", "Dispatch", hltb_minutes=555)

        response = self.client.get(reverse("medialist", args=[MediaTypes.GAME.value]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "toggleSort('time_to_beat')")

    def test_movie_sort_dropdown_includes_runtime_option(self):
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "toggleSort('runtime')")

    def test_supported_media_sort_dropdown_includes_popularity_option(self):
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "toggleSort('popularity')")

    def test_media_sort_dropdown_is_alphabetized(self):
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        labels = [str(label) for _value, label in response.context["sort_choices"]]
        self.assertEqual(labels, sorted(labels, key=str.lower))

    def test_supported_media_sort_dropdown_includes_critic_rating_option(self):
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "toggleSort('critic_rating')")

    def test_non_game_sort_hides_time_to_beat_option_and_falls_back(self):
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?sort=time_to_beat",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('time_to_beat')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_sort, "title")

    def test_non_runtime_media_sort_hides_runtime_option_and_falls_back(self):
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?sort=runtime",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('runtime')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.book_sort, "title")

    def test_non_popularity_media_sort_hides_popularity_option_and_falls_back(self):
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?sort=popularity",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('popularity')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.book_sort, "title")

    def test_non_supported_critic_rating_sort_hides_option_and_falls_back(self):
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.PODCAST.value]) + "?sort=critic_rating",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('critic_rating')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.podcast_sort, "title")

    def test_movie_sort_by_plays_orders_by_aggregated_completed_plays(self):
        """Movie plays sort should use aggregated completed play totals."""
        item = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        older = timezone.now() - timedelta(days=30)
        newer = timezone.now() - timedelta(days=3)
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=0,
                    end_date=older,
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=newer,
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?sort=plays&direction=desc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "plays")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "Test Movie 1")

    def test_movie_sort_by_time_watched_orders_by_total_minutes(self):
        """Movie time watched sort should prioritize plays multiplied by runtime."""
        item_one = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        item_two = Item.objects.get(
            title="Test Movie 2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )

        Item.objects.filter(id=item_one.id).update(runtime_minutes=30)
        Item.objects.filter(id=item_two.id).update(runtime_minutes=100)

        Movie.objects.bulk_create(
            [
                Movie(
                    item=item_one,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=timezone.now() - timedelta(days=7),
                ),
                Movie(
                    item=item_one,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=timezone.now() - timedelta(days=6),
                ),
                Movie(
                    item=item_one,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=timezone.now() - timedelta(days=5),
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&sort=time_watched&direction=desc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "time_watched")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "Test Movie 1")
        self.assertContains(response, "2h 00min")

    def test_tv_sort_by_plays_orders_by_episode_progress(self):
        """TV plays sort should use total watched episode progress."""
        first_item = Item.objects.create(
            media_id="tv-plays-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="TV Plays First",
            image="http://example.com/tv1.jpg",
        )
        second_item = Item.objects.create(
            media_id="tv-plays-2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="TV Plays Second",
            image="http://example.com/tv2.jpg",
        )
        first_tv = TV.objects.create(
            item=first_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        second_tv = TV.objects.create(
            item=second_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        first_season_item = Item.objects.create(
            media_id="tv-plays-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Plays First",
            image="http://example.com/tv1-season.jpg",
            season_number=1,
        )
        second_season_item = Item.objects.create(
            media_id="tv-plays-2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Plays Second",
            image="http://example.com/tv2-season.jpg",
            season_number=1,
        )
        first_season = Season.objects.create(
            item=first_season_item,
            user=self.user,
            related_tv=first_tv,
            status=Status.IN_PROGRESS.value,
        )
        second_season = Season.objects.create(
            item=second_season_item,
            user=self.user,
            related_tv=second_tv,
            status=Status.IN_PROGRESS.value,
        )

        first_episode_rows = []
        for episode_number in (1, 2):
            episode_item = Item.objects.create(
                media_id="tv-plays-1",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"TV Plays First Episode {episode_number}",
                image="http://example.com/tv1-episode.jpg",
                season_number=1,
                episode_number=episode_number,
            )
            first_episode_rows.append(
                Episode(
                    item=episode_item,
                    related_season=first_season,
                    end_date=timezone.now() - timedelta(days=episode_number),
                ),
            )
        Episode.objects.bulk_create(first_episode_rows)

        episode_item = Item.objects.create(
            media_id="tv-plays-2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="TV Plays Second Episode 1",
            image="http://example.com/tv2-episode.jpg",
            season_number=1,
            episode_number=1,
        )
        Episode.objects.bulk_create(
            [
                Episode(
                    item=episode_item,
                    related_season=second_season,
                    end_date=timezone.now() - timedelta(days=1),
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value]) + "?sort=plays&direction=desc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "plays")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "TV Plays First")

    def test_movie_sort_by_release_date_orders_items(self):
        """Release date sort should order by item.release_datetime."""
        now = timezone.now()
        item1 = Item.objects.get(
            title="Test Movie 1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        item2 = Item.objects.get(
            title="Test Movie 2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        item3 = Item.objects.get(
            title="Test Movie 3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
        )
        Item.objects.filter(id=item1.id).update(release_datetime=now - timedelta(days=90))
        Item.objects.filter(id=item2.id).update(release_datetime=now - timedelta(days=15))
        Item.objects.filter(id=item3.id).update(release_datetime=now - timedelta(days=45))

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?sort=release_date&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "release_date")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "Test Movie 1")

    def test_movie_sort_by_date_added_orders_items(self):
        """Date added sort should order by media.created_at."""
        oldest = timezone.now() - timedelta(days=120)
        newest = timezone.now() - timedelta(days=3)
        middle = timezone.now() - timedelta(days=40)

        movie1 = Movie.objects.get(item__title="Test Movie 1", user=self.user)
        movie2 = Movie.objects.get(item__title="Test Movie 2", user=self.user)
        movie3 = Movie.objects.get(item__title="Test Movie 3", user=self.user)
        Movie.objects.filter(id=movie1.id).update(created_at=oldest)
        Movie.objects.filter(id=movie2.id).update(created_at=newest)
        Movie.objects.filter(id=movie3.id).update(created_at=middle)

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?sort=date_added&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "date_added")
        self.assertEqual(response.context["media_list"].object_list[0].item.title, "Test Movie 1")

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        self.assertEqual(response.context["media_list"].paginator.count, 2)

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_status_all_includes_collected_untracked_items(self):
        tracked_item = Item.objects.create(
            media_id="status-all-collected-tracked",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Status All Collected Tracked",
            image="http://example.com/status-all-tracked.jpg",
        )
        Movie.objects.create(
            item=tracked_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        untracked_item = Item.objects.create(
            media_id="status-all-collected-untracked",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Status All Collected Untracked",
            image="http://example.com/status-all-untracked.jpg",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=untracked_item,
            media_type="digital",
        )

        url = reverse("medialist", args=[MediaTypes.MOVIE.value])
        response = self.client.get(url, {"search": "Status All Collected"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry.item.title for entry in response.context["media_list"].object_list],
            [
                "Status All Collected Tracked",
                "Status All Collected Untracked",
            ],
        )

        in_progress_response = self.client.get(
            url,
            {
                "search": "Status All Collected",
                "status": Status.IN_PROGRESS.value,
            },
        )
        self.assertEqual(in_progress_response.status_code, 200)
        self.assertEqual(
            [entry.item.title for entry in in_progress_response.context["media_list"].object_list],
            ["Status All Collected Tracked"],
        )

    def test_no_status_filter_shows_only_collected_untracked_without_persisting_preference(self):
        self.user.movie_status = Status.COMPLETED.value
        self.user.save(update_fields=["movie_status"])

        tracked_item = Item.objects.create(
            media_id="no-status-tracked",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="No Status Filter Tracked",
            image="http://example.com/no-status-tracked.jpg",
        )
        Movie.objects.create(
            item=tracked_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
        )

        untracked_item = Item.objects.create(
            media_id="no-status-untracked",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="No Status Filter Untracked",
            image="http://example.com/no-status-untracked.jpg",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=untracked_item,
            media_type="digital",
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]),
            {
                "search": "No Status Filter",
                "status": "no_status",
                "layout": "table",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_status"], "no_status")
        self.assertEqual(
            [entry.item.title for entry in response.context["media_list"].object_list],
            ["No Status Filter Untracked"],
        )
        self.assertContains(response, "No Status")

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)

    def test_not_rated_filter_includes_collected_untracked_items(self):
        rated_item = Item.objects.create(
            media_id="rating-split-tracked",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Rating Split Tracked",
            image="http://example.com/rating-split-tracked.jpg",
        )
        Movie.objects.create(
            item=rated_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
            score=9,
        )

        untracked_item = Item.objects.create(
            media_id="rating-split-untracked",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Rating Split Untracked",
            image="http://example.com/rating-split-untracked.jpg",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=untracked_item,
            media_type="digital",
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]),
            {
                "search": "Rating Split",
                "rating": "not_rated",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_rating"], "not_rated")
        self.assertEqual(
            [entry.item.title for entry in response.context["media_list"].object_list],
            ["Rating Split Untracked"],
        )

    def test_tv_episode_collection_items_appear_for_all_and_no_status(self):
        show_item = Item.objects.create(
            media_id="tv-episode-collected-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Episode Collected Library Show",
            image="http://example.com/episode-collected-show.jpg",
        )
        episode_item = Item.objects.create(
            media_id="tv-episode-collected-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode Collected Library Show Episode 1",
            image="http://example.com/episode-collected-ep.jpg",
            season_number=1,
            episode_number=1,
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=episode_item,
            media_type="digital",
        )

        url = reverse("medialist", args=[MediaTypes.TV.value])
        all_response = self.client.get(url, {"search": "Episode Collected Library"})
        no_status_response = self.client.get(
            url,
            {"search": "Episode Collected Library", "status": "no_status"},
        )

        self.assertEqual(show_item.id, no_status_response.context["media_list"].object_list[0].item.id)
        self.assertEqual(
            [entry.item.title for entry in all_response.context["media_list"].object_list],
            ["Episode Collected Library Show"],
        )
        self.assertEqual(
            [entry.item.title for entry in no_status_response.context["media_list"].object_list],
            ["Episode Collected Library Show"],
        )

    def test_progress_filters_exclude_collected_untracked_items(self):
        self._create_tv_runtime_entry(
            "progress-filter-tracked-tv",
            "Progress Filter Tracked TV",
            [24, 24, 24],
            progress=1,
        )
        episode_item = Item.objects.create(
            media_id="progress-filter-untracked-tv",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Progress Filter Untracked TV Episode 1",
            image="http://example.com/progress-filter-untracked-ep.jpg",
            season_number=1,
            episode_number=1,
        )
        Item.objects.create(
            media_id="progress-filter-untracked-tv",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Progress Filter Untracked TV",
            image="http://example.com/progress-filter-untracked-show.jpg",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=episode_item,
            media_type="digital",
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value]),
            {
                "search": "Progress Filter",
                "status": "All",
                "progress": "not_caught_up",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry.item.title for entry in response.context["media_list"].object_list],
            ["Progress Filter Tracked TV"],
        )

    def test_progress_filter_is_visible_only_for_tv_and_anime(self):
        """Progress filtering should render only where caught-up semantics are supported."""
        movie_response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))
        tv_response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))
        anime_response = self.client.get(reverse("medialist", args=[MediaTypes.ANIME.value]))

        self.assertFalse(movie_response.context["filter_data"]["show_progress"])
        self.assertTrue(tv_response.context["filter_data"]["show_progress"])
        self.assertTrue(anime_response.context["filter_data"]["show_progress"])

        self.assertNotContains(movie_response, "@click=\"view = 'progress'\"")
        self.assertContains(tv_response, "@click=\"view = 'progress'\"")
        self.assertContains(anime_response, "@click=\"view = 'progress'\"")

    def test_tv_progress_filter_hides_caught_up_shows(self):
        """TV caught-up filtering should split shows by watched-vs-released progress."""
        self._create_tv_runtime_entry(
            "tv-progress-caught-up",
            "Caught Up TV",
            [24, 24, 24],
            progress=3,
        )
        self._create_tv_runtime_entry(
            "tv-progress-not-caught-up",
            "Not Caught Up TV",
            [24, 24, 24],
            progress=1,
        )

        url = reverse("medialist", args=[MediaTypes.TV.value])

        not_caught_up_response = self.client.get(f"{url}?progress=not_caught_up")
        self.assertEqual(not_caught_up_response.status_code, 200)
        self.assertTrue(not_caught_up_response.context["filter_data"]["show_progress"])
        self.assertEqual(not_caught_up_response.context["current_progress"], "not_caught_up")
        self.assertEqual(not_caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            [media.item.title for media in not_caught_up_response.context["media_list"].object_list],
            ["Not Caught Up TV"],
        )

        caught_up_response = self.client.get(f"{url}?progress=caught_up")
        self.assertEqual(caught_up_response.status_code, 200)
        self.assertEqual(caught_up_response.context["current_progress"], "caught_up")
        self.assertEqual(caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            [media.item.title for media in caught_up_response.context["media_list"].object_list],
            ["Caught Up TV"],
        )

    def test_tv_progress_filter_ignores_dropped_seasons(self):
        """Dropped seasons should not keep a fully released show out of caught-up."""
        self._create_tv_seasonal_entry(
            "tv-progress-dropped-seasons",
            "Dropped Seasons Caught Up",
            [
                {
                    "season_number": 1,
                    "status": Status.DROPPED.value,
                    "released_episodes": 2,
                    "watched_episodes": 0,
                },
                {
                    "season_number": 2,
                    "status": Status.DROPPED.value,
                    "released_episodes": 2,
                    "watched_episodes": 0,
                },
                {
                    "season_number": 3,
                    "status": Status.COMPLETED.value,
                    "released_episodes": 3,
                    "watched_episodes": 3,
                },
                {
                    "season_number": 4,
                    "status": Status.IN_PROGRESS.value,
                    "released_episodes": 2,
                    "watched_episodes": 2,
                },
            ],
        )
        self._create_tv_runtime_entry(
            "tv-progress-not-caught-up-2",
            "Still In Progress TV",
            [24, 24, 24],
            progress=1,
        )

        url = reverse("medialist", args=[MediaTypes.TV.value])

        caught_up_response = self.client.get(f"{url}?progress=caught_up")
        self.assertEqual(caught_up_response.status_code, 200)
        self.assertEqual(caught_up_response.context["current_progress"], "caught_up")
        self.assertEqual(caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            [media.item.title for media in caught_up_response.context["media_list"].object_list],
            ["Dropped Seasons Caught Up"],
        )

        not_caught_up_response = self.client.get(f"{url}?progress=not_caught_up")
        self.assertEqual(not_caught_up_response.status_code, 200)
        self.assertEqual(not_caught_up_response.context["current_progress"], "not_caught_up")
        self.assertEqual(not_caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            [media.item.title for media in not_caught_up_response.context["media_list"].object_list],
            ["Still In Progress TV"],
        )

    def test_anime_progress_filter_hides_caught_up_shows(self):
        """Anime caught-up filtering should split shows by watched-vs-released progress."""
        self._create_anime_runtime_entry(
            "anime-progress-caught-up",
            "Caught Up Anime",
            runtime_minutes=24,
            episode_count=12,
            progress=12,
        )
        self._create_anime_runtime_entry(
            "anime-progress-not-caught-up",
            "Not Caught Up Anime",
            runtime_minutes=24,
            episode_count=12,
            progress=5,
        )

        url = reverse("medialist", args=[MediaTypes.ANIME.value])

        not_caught_up_response = self.client.get(f"{url}?progress=not_caught_up")
        self.assertEqual(not_caught_up_response.status_code, 200)
        self.assertTrue(not_caught_up_response.context["filter_data"]["show_progress"])
        self.assertEqual(not_caught_up_response.context["current_progress"], "not_caught_up")
        self.assertEqual(not_caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            [media.item.title for media in not_caught_up_response.context["media_list"].object_list],
            ["Not Caught Up Anime"],
        )

        caught_up_response = self.client.get(f"{url}?progress=caught_up")
        self.assertEqual(caught_up_response.status_code, 200)
        self.assertEqual(caught_up_response.context["current_progress"], "caught_up")
        self.assertEqual(caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            [media.item.title for media in caught_up_response.context["media_list"].object_list],
            ["Caught Up Anime"],
        )

    def test_time_left_cache_key_separates_progress_filters(self):
        """Time-left caching should not bleed across different progress filter states."""
        cache_utils.clear_time_left_cache_for_user(self.user.id)

        self._create_tv_runtime_entry(
            "tv-time-left-caught-up",
            "Caught Up Time Left",
            [24, 24, 24],
            progress=3,
        )
        self._create_tv_runtime_entry(
            "tv-time-left-not-caught-up",
            "Not Caught Up Time Left",
            [24, 24, 24],
            progress=1,
        )

        url = reverse("medialist", args=[MediaTypes.TV.value])

        caught_up_response = self.client.get(
            f"{url}?sort=time_left&direction=asc&progress=caught_up",
        )
        self.assertEqual(caught_up_response.status_code, 200)
        self.assertEqual(caught_up_response.context["current_progress"], "caught_up")
        self.assertEqual(caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            caught_up_response.context["media_list"].object_list[0].item.title,
            "Caught Up Time Left",
        )

        not_caught_up_response = self.client.get(
            f"{url}?sort=time_left&direction=asc&progress=not_caught_up",
        )
        self.assertEqual(not_caught_up_response.status_code, 200)
        self.assertEqual(not_caught_up_response.context["current_progress"], "not_caught_up")
        self.assertEqual(not_caught_up_response.context["media_list"].paginator.count, 1)
        self.assertEqual(
            not_caught_up_response.context["media_list"].object_list[0].item.title,
            "Not Caught Up Time Left",
        )

    def test_media_list_with_release_filters(self):
        """Release filter should split tracked media by today."""
        now = timezone.now()
        released_item = (
            Item.objects.filter(
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title="Test Movie 1",
            )
            .only("id")
            .first()
        )
        upcoming_item = (
            Item.objects.filter(
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title="Test Movie 2",
            )
            .only("id")
            .first()
        )
        self.assertIsNotNone(released_item)
        self.assertIsNotNone(upcoming_item)
        Item.objects.filter(id=released_item.id).update(
            release_datetime=now - timedelta(days=30),
        )
        Item.objects.filter(id=upcoming_item.id).update(
            release_datetime=now + timedelta(days=30),
        )

        url = reverse("medialist", args=[MediaTypes.MOVIE.value])

        released_response = self.client.get(f"{url}?release=released")
        self.assertEqual(released_response.status_code, 200)
        self.assertEqual(released_response.context["current_release"], "released")
        self.assertEqual(released_response.context["media_list"].paginator.count, 1)
        self.assertContains(released_response, "Test Movie 1")
        self.assertNotContains(released_response, "Test Movie 2")

        not_released_response = self.client.get(f"{url}?release=not_released")
        self.assertEqual(not_released_response.status_code, 200)
        self.assertEqual(
            not_released_response.context["current_release"],
            "not_released",
        )
        self.assertEqual(not_released_response.context["media_list"].paginator.count, 4)
        self.assertContains(not_released_response, "Test Movie 2")
        self.assertNotContains(not_released_response, "Test Movie 1")

    def test_game_platform_filter_prefers_collection_resolution(self):
        """Game platform filtering should prefer collection platform over metadata platforms."""
        switch_override_item = Item.objects.create(
            media_id="game-platform-filter-1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Multiplatform Game",
            image="http://example.com/game1.jpg",
            platforms=["PlayStation 5"],
        )
        ps5_item = Item.objects.create(
            media_id="game-platform-filter-2",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="PS5 Exclusive Game",
            image="http://example.com/game2.jpg",
            platforms=["PlayStation 5"],
        )

        Game.objects.bulk_create(
            [
                Game(
                    item=switch_override_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=60,
                ),
                Game(
                    item=ps5_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=60,
                ),
            ],
        )

        CollectionEntry.objects.create(
            user=self.user,
            item=switch_override_item,
            resolution="Nintendo Switch",
        )

        url = reverse("medialist", args=[MediaTypes.GAME.value])
        response = self.client.get(url, {"platform": "PlayStation 5", "status": "All"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "PS5 Exclusive Game")
        self.assertNotContains(response, "Multiplatform Game")

        platform_values = {
            option["value"] for option in response.context["filter_data"]["platforms"]
        }
        self.assertIn("Nintendo Switch", platform_values)
        self.assertIn("PlayStation 5", platform_values)

    def test_game_table_renders_time_to_beat_column(self):
        self._create_game_entry("325609", "Dispatch", hltb_minutes=555)

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.GAME.value]) + "?layout=table",
        )

        self.assertEqual(response.status_code, 200)
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertIn("time_to_beat", column_keys)
        self.assertContains(response, "Time to Beat")
        self.assertContains(response, "9h 15min")

    def test_movie_table_renders_runtime_column(self):
        self._create_movie_runtime_entry("runtime-column-movie", "Runtime Column Movie", 142)

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=table&search=Runtime+Column+Movie",
        )

        self.assertEqual(response.status_code, 200)
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertIn("runtime", column_keys)
        self.assertContains(response, "Runtime")
        self.assertContains(response, "2h 22min")

    def test_movie_table_renders_time_watched_column(self):
        item = Item.objects.create(
            media_id="time-watched-column-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Time Watched Column Movie",
            image="http://example.com/time-watched-column.jpg",
            runtime_minutes=142,
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=timezone.now() - timedelta(days=2),
                ),
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=1,
                    end_date=timezone.now() - timedelta(days=1),
                ),
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=table&search=Time+Watched+Column+Movie",
        )

        self.assertEqual(response.status_code, 200)
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertIn("time_watched", column_keys)
        self.assertContains(response, "Time Watched")
        self.assertContains(response, "4h 44min")

    def test_movie_table_renders_popularity_column(self):
        self._create_movie_popularity_entry("popularity-column-movie", "Popularity Column Movie", 7)

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=table&search=Popularity+Column+Movie",
        )

        self.assertEqual(response.status_code, 200)
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertIn("popularity", column_keys)
        self.assertContains(response, "Popularity")
        self.assertContains(response, "#7")

    def test_movie_table_renders_critic_rating_column(self):
        self._create_movie_critic_rating_entry(
            "critic-column-movie",
            "Critic Column Movie",
            8.7,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=table&search=Critic+Column+Movie",
        )

        self.assertEqual(response.status_code, 200)
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertIn("critic_rating", column_keys)
        self.assertContains(response, "Critic Rating")
        self.assertContains(response, "8.7")

    def test_movie_sort_by_runtime_orders_shortest_first(self):
        self._create_movie_runtime_entry("runtime-sort-movie-1", "Runtime Sort Long", 160)
        self._create_movie_runtime_entry("runtime-sort-movie-2", "Runtime Sort Short", 95)
        self._create_movie_runtime_entry("runtime-sort-movie-3", "Runtime Sort Unknown")

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Runtime+Sort&sort=runtime",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "runtime")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:3]],
            ["Runtime Sort Short", "Runtime Sort Long", "Runtime Sort Unknown"],
        )
        self.assertContains(response, "1h 35min")
        self.assertContains(response, "2h 40min")

    def test_movie_sort_by_popularity_orders_lowest_rank_first(self):
        self._create_movie_popularity_entry("popularity-sort-movie-1", "Popularity Rank Two", 2)
        self._create_movie_popularity_entry("popularity-sort-movie-2", "Popularity Rank One", 1)
        self._create_movie_popularity_entry("popularity-sort-movie-3", "Popularity Rank Missing", 50)
        Item.objects.filter(media_id="popularity-sort-movie-3").update(
            trakt_popularity_rank=None,
            trakt_popularity_score=None,
            trakt_rating=None,
            trakt_rating_count=None,
            trakt_popularity_fetched_at=None,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Popularity+Rank&sort=popularity",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "popularity")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:3]],
            ["Popularity Rank One", "Popularity Rank Two", "Popularity Rank Missing"],
        )

    def test_movie_sort_by_critic_rating_orders_highest_first(self):
        self._create_movie_critic_rating_entry(
            "critic-sort-movie-1",
            "Critic Rating Low",
            7.2,
        )
        self._create_movie_critic_rating_entry(
            "critic-sort-movie-2",
            "Critic Rating High",
            9.1,
        )
        self._create_movie_critic_rating_entry(
            "critic-sort-movie-3",
            "Critic Rating Missing",
            None,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?layout=grid&search=Critic+Rating&sort=critic_rating",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "critic_rating")
        self.assertEqual(response.context["current_direction"], "desc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:3]],
            ["Critic Rating High", "Critic Rating Low", "Critic Rating Missing"],
        )
        self.assertContains(response, "9.1/10")
        self.assertContains(response, "7.2/10")

    def test_tv_sort_by_runtime_uses_total_show_runtime(self):
        self._create_tv_runtime_entry("tv-runtime-1", "TV Runtime Short", [24, 24, 24])
        self._create_tv_runtime_entry("tv-runtime-2", "TV Runtime Long", [48, 48, 48])

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value])
            + "?layout=grid&search=TV+Runtime&sort=runtime",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "runtime")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:2]],
            ["TV Runtime Short", "TV Runtime Long"],
        )
        self.assertContains(response, "1h 12min")
        self.assertContains(response, "2h 24min")

    def test_anime_sort_by_runtime_uses_episode_count_times_runtime(self):
        self._create_anime_runtime_entry(
            "anime-runtime-1",
            "Anime Runtime Short",
            runtime_minutes=24,
            episode_count=12,
        )
        self._create_anime_runtime_entry(
            "anime-runtime-2",
            "Anime Runtime Long",
            runtime_minutes=24,
            episode_count=26,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.ANIME.value])
            + "?layout=grid&search=Anime+Runtime&sort=runtime",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "runtime")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:2]],
            ["Anime Runtime Short", "Anime Runtime Long"],
        )
        self.assertContains(response, "4h 48min")
        self.assertContains(response, "10h 24min")

    def test_tv_and_season_sort_by_next_episode_air_date_orders_items(self):
        self.user.date_format = "iso_8601"
        self.user.save(update_fields=["date_format"])

        base_now = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        backlog_release = base_now - timedelta(days=10)
        future_release = base_now + timedelta(days=10)

        self._create_tv_next_episode_air_date_entry(
            "tv-next-episode-1",
            "Zulu Backlog TV",
            [backlog_release, backlog_release + timedelta(days=7)],
            progress=0,
        )
        self._create_tv_next_episode_air_date_entry(
            "tv-next-episode-2",
            "Alpha Future TV",
            [backlog_release, future_release],
            progress=1,
        )
        self._create_tv_next_episode_air_date_entry(
            "tv-next-episode-3",
            "Mike Missing TV",
            [None, future_release + timedelta(days=7)],
            progress=0,
        )

        tv_response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value])
            + "?layout=grid&sort=next_episode_air_date",
        )

        self.assertEqual(tv_response.status_code, 200)
        self.assertEqual(tv_response.context["current_sort"], "next_episode_air_date")
        self.assertEqual(tv_response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in tv_response.context["media_list"].object_list[:3]],
            ["Zulu Backlog TV", "Alpha Future TV", "Mike Missing TV"],
        )
        self.assertContains(tv_response, timezone.localtime(backlog_release).date().isoformat())
        self.assertContains(tv_response, timezone.localtime(future_release).date().isoformat())

        season_response = self.client.get(
            reverse("medialist", args=[MediaTypes.SEASON.value])
            + "?layout=table&sort=next_episode_air_date",
        )

        self.assertEqual(season_response.status_code, 200)
        self.assertEqual(season_response.context["current_sort"], "next_episode_air_date")
        self.assertEqual(season_response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in season_response.context["media_list"].object_list[:3]],
            [
                "Zulu Backlog TV Season 1",
                "Alpha Future TV Season 1",
                "Mike Missing TV Season 1",
            ],
        )
        self.assertIn(
            "next_episode_air_date",
            [column.key for column in season_response.context["resolved_columns"]],
        )
        self.assertContains(season_response, "Episode Air Date")
        self.assertContains(
            season_response,
            timezone.localtime(backlog_release).date().isoformat(),
        )
        self.assertContains(
            season_response,
            timezone.localtime(future_release).date().isoformat(),
        )

    def test_anime_sort_by_next_episode_air_date_orders_grouped_and_flat_rows(self):
        self.user.date_format = "iso_8601"
        self.user.anime_library_mode = MediaTypes.ANIME.value
        self.user.save(update_fields=["date_format", "anime_library_mode"])

        base_now = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        past_release = base_now - timedelta(days=14)
        future_release = base_now + timedelta(days=14)

        self._create_anime_next_episode_air_date_entry(
            "anime-next-episode-1",
            "Flat Past Anime",
            [(1, past_release)],
            progress=0,
        )
        self._create_tv_next_episode_air_date_entry(
            "anime-next-episode-2",
            "Grouped Future Anime",
            [past_release, future_release],
            progress=1,
            library_media_type=MediaTypes.ANIME.value,
        )
        missing_item = Item.objects.create(
            media_id="anime-next-episode-3",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Flat Missing Anime",
            image="http://example.com/anime-missing.jpg",
        )
        Anime.objects.create(
            item=missing_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.ANIME.value])
            + "?layout=grid&sort=next_episode_air_date",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "next_episode_air_date")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:3]],
            ["Flat Past Anime", "Grouped Future Anime", "Flat Missing Anime"],
        )
        self.assertContains(response, timezone.localtime(past_release).date().isoformat())
        self.assertContains(response, timezone.localtime(future_release).date().isoformat())

    def test_game_sort_by_time_to_beat_orders_by_best_available_value(self):
        self._create_game_entry("910001", "Longest Run", hltb_minutes=555)
        self._create_game_entry("910002", "Quick Quest", hltb_minutes=120)
        self._create_game_entry("910003", "Fallback Route", igdb_seconds=10800)
        self._create_game_entry("910004", "Unknown Length")

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.GAME.value])
            + "?layout=grid&sort=time_to_beat&direction=asc",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "time_to_beat")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:4]],
            ["Quick Quest", "Fallback Route", "Longest Run", "Unknown Length"],
        )
        self.assertContains(response, "2h 00min")
        self.assertContains(response, "3h 00min")

    def test_game_platform_filter_uses_latest_aggregated_status(self):
        """Status filtering should honor latest aggregated status for duplicate sessions."""
        stale_item = Item.objects.create(
            media_id="game-platform-filter-latest-1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Completed Now, Was In Progress",
            image="http://example.com/game-latest-1.jpg",
            platforms=["PlayStation 5"],
        )
        active_item = Item.objects.create(
            media_id="game-platform-filter-latest-2",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Still In Progress",
            image="http://example.com/game-latest-2.jpg",
            platforms=["PlayStation 5"],
        )

        Game.objects.bulk_create(
            [
                Game(
                    item=stale_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=12,
                ),
                Game(
                    item=stale_item,
                    user=self.user,
                    status=Status.COMPLETED.value,
                    progress=30,
                    end_date=timezone.now() - timedelta(days=1),
                ),
                Game(
                    item=active_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=20,
                ),
            ],
        )

        old_in_progress = Game.objects.filter(
            item=stale_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        ).get()
        old_activity = timezone.now() - timedelta(days=3)
        Game.objects.filter(id=old_in_progress.id).update(
            created_at=old_activity,
            progressed_at=old_activity,
        )

        url = reverse("medialist", args=[MediaTypes.GAME.value])
        response = self.client.get(
            url,
            {"platform": "PlayStation 5", "status": Status.IN_PROGRESS.value},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media_list"].paginator.count, 1)
        self.assertContains(response, "Still In Progress")
        self.assertNotContains(response, "Completed Now, Was In Progress")

    def test_book_format_filter_uses_collection_entry_media_type(self):
        """Book format options should include collection-only formats like Audiobook."""
        book_item = Item.objects.create(
            media_id="book-audiobook-filter",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Audiobook Filter Book",
            image="http://example.com/book.jpg",
            format="",
        )
        Book.objects.bulk_create(
            [
                Book(
                    item=book_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                ),
            ],
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=book_item,
            media_type="Audiobook",
        )

        url = reverse("medialist", args=[MediaTypes.BOOK.value])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_data"]["show_formats"])
        self.assertTrue(
            any(
                option["value"] == "audiobook"
                and option["label"] == "Audiobook"
                for option in response.context["filter_data"]["formats"]
            ),
        )

        filtered_response = self.client.get(f"{url}?format=audiobook")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertEqual(filtered_response.context["current_format"], "audiobook")
        self.assertEqual(filtered_response.context["media_list"].paginator.count, 1)
        self.assertContains(filtered_response, "Audiobook Filter Book")

    def test_book_author_filter_shows_and_filters_tracked_books(self):
        book_with_author = Item.objects.create(
            media_id="book-author-filter-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Filter Book One",
            image="http://example.com/book1.jpg",
            authors=["Author One"],
        )
        other_book = Item.objects.create(
            media_id="book-author-filter-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Filter Book Two",
            image="http://example.com/book2.jpg",
            authors=["Author Two"],
        )
        Book.objects.create(
            item=book_with_author,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        Book.objects.create(
            item=other_book,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        url = reverse("medialist", args=[MediaTypes.BOOK.value])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_data"]["show_authors"])
        self.assertTrue(
            any(
                option["value"] == "Author One"
                for option in response.context["filter_data"]["authors"]
            ),
        )

        filtered_response = self.client.get(f"{url}?author=Author One")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertEqual(filtered_response.context["current_author"], "Author One")
        self.assertEqual(filtered_response.context["media_list"].paginator.count, 1)
        self.assertContains(filtered_response, "Author Filter Book One")
        self.assertNotContains(filtered_response, "Author Filter Book Two")

    def test_book_sort_by_author_orders_alphabetically(self):
        first_book = Item.objects.create(
            media_id="book-author-sort-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Zulu Title",
            image="http://example.com/book-sort-1.jpg",
            authors=["Bravo Writer"],
        )
        second_book = Item.objects.create(
            media_id="book-author-sort-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Alpha Title",
            image="http://example.com/book-sort-2.jpg",
            authors=["Alpha Writer"],
        )
        Book.objects.create(
            item=first_book,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        Book.objects.create(
            item=second_book,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?sort=author",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "author")
        self.assertEqual(response.context["current_direction"], "asc")
        self.assertContains(response, "toggleSort('author')")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:2]],
            ["Alpha Title", "Zulu Title"],
        )

    def test_book_table_renders_author_column(self):
        book_item = Item.objects.create(
            media_id="book-author-column-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Author Column Book",
            image="http://example.com/book-column.jpg",
            authors=["Author One", "Author Two"],
        )
        Book.objects.create(
            item=book_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.BOOK.value]) + "?layout=table",
        )

        self.assertEqual(response.status_code, 200)
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertIn("author", column_keys)
        self.assertContains(response, "Author One, Author Two")

    def test_comic_and_manga_author_filter_work(self):
        comic_item = Item.objects.create(
            media_id="comic-author-filter-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.COMIC.value,
            title="Author Filter Comic",
            image="http://example.com/comic.jpg",
            authors=["Writer Alpha"],
        )
        manga_item = Item.objects.create(
            media_id="manga-author-filter-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Author Filter Manga",
            image="http://example.com/manga.jpg",
            authors=["Mangaka Beta"],
        )
        Comic.objects.create(
            item=comic_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        Manga.objects.create(
            item=manga_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        comic_url = reverse("medialist", args=[MediaTypes.COMIC.value])
        comic_response = self.client.get(f"{comic_url}?author=Writer Alpha")
        self.assertEqual(comic_response.status_code, 200)
        self.assertTrue(comic_response.context["filter_data"]["show_authors"])
        self.assertEqual(comic_response.context["media_list"].paginator.count, 1)
        self.assertContains(comic_response, "Author Filter Comic")

        manga_url = reverse("medialist", args=[MediaTypes.MANGA.value])
        manga_response = self.client.get(f"{manga_url}?author=Mangaka Beta")
        self.assertEqual(manga_response.status_code, 200)
        self.assertTrue(manga_response.context["filter_data"]["show_authors"])
        self.assertEqual(manga_response.context["media_list"].paginator.count, 1)
        self.assertContains(manga_response, "Author Filter Manga")

    def test_movie_filter_data_hides_author_filter(self):
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["filter_data"]["show_authors"])

    def test_non_bookish_sort_hides_author_option_and_falls_back(self):
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?sort=author",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "title")
        self.assertNotContains(response, "toggleSort('author')")

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_sort, "title")

    def test_media_list_filter_persistence_serializes_filter_form_fields(self):
        """Filter persistence should derive keys from the hidden filter form."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?tag=Favorite&tag_exclude=Archived&layout=grid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_tag"], "Favorite")
        self.assertEqual(response.context["current_tag_exclude"], "Archived")
        self.assertContains(response, "function buildMediaListFilterParams(form, overrides = {})")
        self.assertContains(response, "Array.from(form.elements)")
        self.assertContains(response, "const persistedKeys = Array.from(")
        self.assertNotContains(response, "const persistedKeys = [")

    def test_media_list_layout_toggle_uses_shared_filter_serializer(self):
        """Grid/table links should reuse the shared serializer instead of manual query strings."""
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ":href=\"layoutHref('grid')\"")
        self.assertContains(response, ":href=\"layoutHref('table')\"")
        self.assertContains(
            response,
            "layoutHref(nextLayout) { return buildMediaListHref(this.mediaListUrl, document.getElementById('filter-form'), { layout: nextLayout }); }",
        )

    def test_comic_media_list_can_switch_to_issue_subview(self):
        """Comic media list should reuse the music-style subview switch for issues."""
        comic_item = Item.objects.create(
            media_id="comic-1",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
            title="Volume One",
            image="http://example.com/comic.jpg",
        )
        Comic.objects.create(
            item=comic_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
        )

        issue_item = Item.objects.create(
            media_id="issue-1",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC_ISSUE.value,
            title="Issue One",
            image="http://example.com/issue.jpg",
        )
        ComicIssue.objects.create(
            item=issue_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.COMIC.value]) + "?subview=issues",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_subview"], "issues")
        self.assertEqual(response.context["media_type"], MediaTypes.COMIC.value)
        self.assertEqual(response.context["media_type_plural"], "comic issues")
        self.assertContains(response, "?subview=comics")
        self.assertContains(response, "?subview=issues")
        self.assertContains(response, "Issue One")
        self.assertNotContains(response, "Volume One")
        self.assertEqual(
            response.context["media_list"].object_list[0].item.media_type,
            MediaTypes.COMIC_ISSUE.value,
        )

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=grid",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/table_items.html")

    def test_table_column_refresh_wiring_is_present(self):
        """Table layout should render deterministic column refresh wiring."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )

        self.assertContains(response, 'id="column-pref-form"')
        self.assertContains(response, "this.$refs.form.addEventListener('htmx:afterRequest'")
        self.assertContains(response, "htmx.ajax('GET'")
        self.assertContains(response, "column_refresh_nonce")
        self.assertContains(response, "save_after_request successful=")
        self.assertContains(response, "refresh_dispatch source=save_after_request")
        self.assertNotContains(response, 'id="column-refresh-runner"')
        self.assertNotContains(response, 'hx-trigger="runColumnRefresh"')

    def test_table_header_and_row_cells_match_for_pagination(self):
        """Table pagination rows should always match header column count."""
        extra_ids = [f"extra-{i}" for i in range(6, 41)]
        Item.objects.bulk_create(
            [
                Item(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"Extra Movie {media_id}",
                    image="http://example.com/image.jpg",
                )
                for media_id in extra_ids
            ],
        )
        extra_items = {
            item.media_id: item
            for item in Item.objects.filter(
                media_id__in=extra_ids,
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            )
        }
        Movie.objects.bulk_create(
            [
                Movie(
                    item=extra_items[media_id],
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                    score=5,
                )
                for media_id in extra_ids
            ],
        )

        headers = {"HTTP_HX_REQUEST": "true"}
        first_page = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table&page=1",
            **headers,
        )
        first_html = first_page.content.decode()
        header_count = first_html.count("<th ")
        self.assertGreater(header_count, 0)

        first_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", first_html, flags=re.S)
        self.assertGreater(len(first_rows), 0)
        for row_html in first_rows:
            if "<td " not in row_html:
                continue
            self.assertEqual(row_html.count("<td "), header_count)

        second_page = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table&page=2",
            **headers,
        )
        second_html = second_page.content.decode()
        self.assertNotIn("<thead", second_html)

        second_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", second_html, flags=re.S)
        self.assertGreater(len(second_rows), 0)
        for row_html in second_rows:
            self.assertEqual(row_html.count("<td "), header_count)

    def test_column_preferences_endpoint_updates_user_prefs(self):
        """Column preference updates should persist and trigger table refresh."""
        response = self.client.post(
            reverse("medialist_columns", args=[MediaTypes.MOVIE.value]),
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["status"]),
                "hidden": json.dumps(["status"]),
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("HX-Trigger", response)
        self.assertIn("refreshTableColumns", response["HX-Trigger"])

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.MOVIE.value],
            {
                "order": [
                    "status",
                    "score",
                    "critic_rating",
                    "runtime",
                    "time_watched",
                    "popularity",
                    "release_date",
                    "date_added",
                    "start_date",
                    "end_date",
                ],
                "hidden": ["status"],
            },
        )

    def test_table_columns_keep_fixed_columns_at_front_after_save(self):
        """Saving prefs without fixed columns in order keeps them anchored first."""
        self.client.post(
            reverse("medialist_columns", args=[MediaTypes.MOVIE.value]),
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["end_date", "status"]),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertEqual(column_keys[:2], ["image", "title"])
        self.assertEqual(column_keys[2:4], ["end_date", "status"])

    def test_column_preferences_second_save_wins(self):
        """Consecutive saves should persist and render the latest submitted order."""
        url = reverse("medialist_columns", args=[MediaTypes.MOVIE.value])
        first = self.client.post(
            url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["status", "end_date"]),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(first.status_code, 204)

        second = self.client.post(
            url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(["score", "start_date"]),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(second.status_code, 204)

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.MOVIE.value]["order"],
            [
                "score",
                "start_date",
                "critic_rating",
                "runtime",
                "time_watched",
                "popularity",
                "status",
                "release_date",
                "date_added",
                "end_date",
            ],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
        )
        column_keys = [column.key for column in response.context["resolved_columns"]]
        self.assertEqual(
            column_keys,
            [
                "image",
                "title",
                "score",
                "start_date",
                "critic_rating",
                "runtime",
                "time_watched",
                "popularity",
                "status",
                "release_date",
                "date_added",
                "end_date",
            ],
        )

    def test_consecutive_column_reorder_full_round_trip(self):
        """Two consecutive column saves should each render after HTMX refresh."""
        columns_url = reverse("medialist_columns", args=[MediaTypes.MOVIE.value])
        list_url = reverse("medialist", args=[MediaTypes.MOVIE.value])
        list_query = "?layout=table&sort=score&direction=desc"
        htmx_headers = {"HTTP_HX_REQUEST": "true"}

        def assert_partial_table_refresh(response, expected_labels):
            self.assertEqual(response.status_code, 200)
            self.assertIn("HX-Trigger", response)
            trigger_payload = json.loads(response["HX-Trigger"])
            self.assertIn("resultCountUpdated", trigger_payload)

            html = response.content.decode()
            labels = [
                re.sub(r"<[^>]+>", "", label).strip()
                for label in re.findall(
                    r"<th\s[^>]*>(.*?)</th>",
                    html,
                    flags=re.DOTALL,
                )
            ]
            self.assertEqual(labels, expected_labels)

        first_order = ["end_date", "status", "score", "start_date"]
        first_save = self.client.post(
            columns_url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(first_order),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(first_save.status_code, 204)
        self.assertIn("HX-Trigger", first_save)
        self.assertEqual(
            json.loads(first_save["HX-Trigger"]),
            {"refreshTableColumns": True},
        )

        first_refresh = self.client.get(f"{list_url}{list_query}", **htmx_headers)
        assert_partial_table_refresh(
            first_refresh,
            [
                "",
                "Title",
                "End Date",
                "Status",
                "Score",
                "Start Date",
                "Critic Rating",
                "Runtime",
                "Time Watched",
                "Popularity",
                "Release Date",
                "Date Added",
            ],
        )

        second_order = ["score", "start_date", "end_date", "status"]
        second_save = self.client.post(
            columns_url,
            {
                "table_type": "media",
                "sort": "score",
                "order": json.dumps(second_order),
                "hidden": json.dumps([]),
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(second_save.status_code, 204)
        self.assertIn("HX-Trigger", second_save)
        self.assertEqual(
            json.loads(second_save["HX-Trigger"]),
            {"refreshTableColumns": True},
        )

        second_refresh = self.client.get(f"{list_url}{list_query}", **htmx_headers)
        assert_partial_table_refresh(
            second_refresh,
            [
                "",
                "Title",
                "Score",
                "Start Date",
                "End Date",
                "Status",
                "Critic Rating",
                "Runtime",
                "Time Watched",
                "Popularity",
                "Release Date",
                "Date Added",
            ],
        )

        full_page = self.client.get(f"{list_url}{list_query}")
        self.assertEqual(full_page.status_code, 200)
        self.assertContains(full_page, 'id="column-pref-form"')
        self.assertContains(full_page, f'hx-post="{columns_url}"')
        self.assertContains(full_page, 'hx-swap="none"')
        self.assertContains(full_page, 'x-data="columnConfigMenu({')
        self.assertContains(full_page, "refreshTableAfterColumnSave(options)")

        full_html = full_page.content.decode()
        config_match = re.search(
            r'<script id="media-column-config-data" type="application/json">'
            r"(.*?)</script>",
            full_html,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(config_match)
        if config_match is not None:
            column_config = json.loads(config_match.group(1))
            self.assertEqual(
                [column["key"] for column in column_config],
                second_order
                + [
                    "critic_rating",
                    "runtime",
                    "time_watched",
                    "popularity",
                    "release_date",
                    "date_added",
                ],
            )

        resolved_keys = [column.key for column in full_page.context["resolved_columns"]]
        self.assertEqual(
            resolved_keys,
            [
                "image",
                "title",
                "score",
                "start_date",
                "end_date",
                "status",
                "critic_rating",
                "runtime",
                "time_watched",
                "popularity",
                "release_date",
                "date_added",
            ],
        )

    def test_grouped_anime_library_mode_routes_grouped_titles(self):
        """Grouped anime TV rows should follow the user's anime library mode."""
        grouped_item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Frieren: Beyond Journey's End",
            image="https://example.com/grouped-anime.jpg",
        )
        TV.objects.create(
            item=grouped_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.user.anime_library_mode = MediaTypes.ANIME.value
        self.user.save(update_fields=["anime_library_mode"])

        anime_response = self.client.get(reverse("medialist", args=[MediaTypes.ANIME.value]))
        tv_response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))

        anime_titles = [media.item.title for media in anime_response.context["media_list"].object_list]
        tv_titles = [media.item.title for media in tv_response.context["media_list"].object_list]

        self.assertIn("Frieren: Beyond Journey's End", anime_titles)
        self.assertNotIn("Frieren: Beyond Journey's End", tv_titles)

        self.user.anime_library_mode = MediaTypes.TV.value
        self.user.save(update_fields=["anime_library_mode"])

        anime_response = self.client.get(reverse("medialist", args=[MediaTypes.ANIME.value]))
        tv_response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))

        anime_titles = [media.item.title for media in anime_response.context["media_list"].object_list]
        tv_titles = [media.item.title for media in tv_response.context["media_list"].object_list]

        self.assertNotIn("Frieren: Beyond Journey's End", anime_titles)
        self.assertIn("Frieren: Beyond Journey's End", tv_titles)

    def test_grouped_anime_sort_by_popularity_uses_shared_rank_field(self):
        grouped_low = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Grouped Anime Low Rank",
            image="https://example.com/grouped-anime-low.jpg",
            trakt_rating=8.0,
            trakt_rating_count=1000,
            trakt_popularity_score=5000,
            trakt_popularity_rank=2,
            trakt_popularity_fetched_at=timezone.now() - timedelta(days=1),
        )
        grouped_high = Item.objects.create(
            media_id="9350139",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Grouped Anime High Rank",
            image="https://example.com/grouped-anime-high.jpg",
            trakt_rating=8.0,
            trakt_rating_count=1000,
            trakt_popularity_score=2500,
            trakt_popularity_rank=8,
            trakt_popularity_fetched_at=timezone.now() - timedelta(days=1),
        )
        TV.objects.create(
            item=grouped_low,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        TV.objects.create(
            item=grouped_high,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.user.anime_library_mode = MediaTypes.ANIME.value
        self.user.save(update_fields=["anime_library_mode"])

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.ANIME.value])
            + "?search=Grouped+Anime&sort=popularity",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "popularity")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:2]],
            ["Grouped Anime Low Rank", "Grouped Anime High Rank"],
        )

    def test_grouped_anime_sort_by_critic_rating_uses_provider_rating(self):
        grouped_low = Item.objects.create(
            media_id="9350140",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Grouped Anime Low Critic",
            image="https://example.com/grouped-anime-low-critic.jpg",
            provider_rating=7.4,
        )
        grouped_high = Item.objects.create(
            media_id="9350141",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Grouped Anime High Critic",
            image="https://example.com/grouped-anime-high-critic.jpg",
            provider_rating=8.9,
        )
        TV.objects.create(
            item=grouped_low,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        TV.objects.create(
            item=grouped_high,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.user.anime_library_mode = MediaTypes.ANIME.value
        self.user.save(update_fields=["anime_library_mode"])

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.ANIME.value])
            + "?search=Grouped+Anime&sort=critic_rating",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "critic_rating")
        self.assertEqual(
            [media.item.title for media in response.context["media_list"].object_list[:2]],
            ["Grouped Anime High Critic", "Grouped Anime Low Critic"],
        )
