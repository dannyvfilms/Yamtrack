import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    CollectionEntry,
    Episode,
    Game,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
    TV,
)
from lists.models import CustomList
from users import home_screen
from users.models import (
    DirectionChoices,
    HomeScreenRow,
    HomeScreenRowTypeChoices,
    HomeSortChoices,
    MediaSortChoices,
)


class HomeScreenViewTests(TestCase):
    """Tests for the Home Screen settings page."""

    def setUp(self):
        self.credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def _set_enabled_media_types(self, *enabled_media_types):
        enabled_set = set(enabled_media_types)
        update_fields = []
        for media_type in MediaTypes.values:
            if media_type == MediaTypes.EPISODE.value:
                continue
            field_name = f"{media_type}_enabled"
            setattr(self.user, field_name, media_type in enabled_set)
            update_fields.append(field_name)
        self.user.save(update_fields=update_fields)

    def test_home_screen_get_only_serializes_enabled_media_types(self):
        self._set_enabled_media_types(MediaTypes.TV.value, MediaTypes.MOVIE.value)

        response = self.client.get(reverse("home_screen"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/home_screen.html")

        sections = json.loads(response.context["home_screen_sections_json"])
        self.assertEqual([section["media_type"] for section in sections], ["tv", "movie"])
        self.assertContains(response, "Home Screen")
        self.assertContains(response, "sections: JSON.parse(")
        self.assertContains(response, "directionChoices: JSON.parse(")
        self.assertNotContains(response, 'sections: [{"media_type":')
        self.assertContains(response, "expanded: false")
        self.assertContains(response, 'x-html="section.icon_svg"')
        self.assertNotContains(response, "section.rows.length === 1")
        self.assertContains(response, "Add Row")
        self.assertContains(response, "Add List")
        self.assertNotContains(response, "Add Library Row")
        self.assertNotContains(response, "Add List / Smart List")
        self.assertNotContains(response, "Add Recently Played Row")
        self.assertNotContains(response, "Enabled")

    def test_home_rows_progress_filter_ignores_dropped_tv_seasons(self):
        """Home not-caught-up rows should ignore dropped TV seasons."""
        self._set_enabled_media_types(MediaTypes.TV.value)

        dropped_caught_up_item = Item.objects.create(
            title="Dropped Seasons Caught Up",
            media_id="home-tv-dropped-caught-up",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/tv-caught-up.jpg",
        )
        dropped_caught_up_tv = TV.objects.create(
            item=dropped_caught_up_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        still_in_progress_item = Item.objects.create(
            title="Still In Progress TV",
            media_id="home-tv-still-in-progress",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/tv-in-progress.jpg",
        )
        still_in_progress_tv = TV.objects.create(
            item=still_in_progress_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        now = timezone.now()
        for tv, title, season_configs in (
            (
                dropped_caught_up_tv,
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
            ),
            (
                still_in_progress_tv,
                "Still In Progress TV",
                [
                    {
                        "season_number": 1,
                        "status": Status.IN_PROGRESS.value,
                        "released_episodes": 3,
                        "watched_episodes": 1,
                    },
                ],
            ),
        ):
            for season_config in season_configs:
                season_number = season_config["season_number"]
                season_item = Item.objects.create(
                    media_id=tv.item.media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.SEASON.value,
                    title=f"{title} Season {season_number}",
                    image="https://example.com/tv-season.jpg",
                    season_number=season_number,
                )
                season = Season.objects.create(
                    item=season_item,
                    user=self.user,
                    related_tv=tv,
                    status=season_config["status"],
                )

                for episode_number in range(1, season_config["released_episodes"] + 1):
                    episode_item = Item.objects.create(
                        media_id=tv.item.media_id,
                        source=Sources.TMDB.value,
                        media_type=MediaTypes.EPISODE.value,
                        title=f"{title} S{season_number:02d}E{episode_number:02d}",
                        image="https://example.com/tv-episode.jpg",
                        season_number=season_number,
                        episode_number=episode_number,
                        release_datetime=now - timedelta(days=episode_number),
                    )
                    if episode_number <= season_config["watched_episodes"]:
                        Episode.objects.create(
                            item=episode_item,
                            related_season=season,
                            end_date=now - timedelta(days=episode_number),
                        )

        HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.TV.value,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=MediaSortChoices.TITLE,
            direction=DirectionChoices.ASC,
            filters={
                "status": Status.IN_PROGRESS.value,
                "progress": "not_caught_up",
            },
        )

        groups = home_screen.build_home_page_groups(self.user, items_limit=10)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["rows"]), 1)
        self.assertEqual(
            [entry.item.title for entry in groups[0]["rows"][0]["items"]],
            ["Still In Progress TV"],
        )

    def test_library_row_status_all_includes_collected_untracked_items(self):
        self._set_enabled_media_types(MediaTypes.MOVIE.value)

        tracked_item = Item.objects.create(
            title="Home Library Tracked Movie",
            media_id="home-library-tracked-movie",
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            image="https://example.com/home-library-tracked.jpg",
        )
        Movie.objects.create(
            item=tracked_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        untracked_item = Item.objects.create(
            title="Home Library Untracked Movie",
            media_id="home-library-untracked-movie",
            media_type=MediaTypes.MOVIE.value,
            source=Sources.TMDB.value,
            image="https://example.com/home-library-untracked.jpg",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=untracked_item,
            media_type="digital",
        )

        HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.MOVIE.value,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=MediaSortChoices.TITLE,
            direction=DirectionChoices.ASC,
            filters={"status": "all"},
        )

        groups = home_screen.build_home_page_groups(self.user, items_limit=10)

        self.assertEqual(
            [entry.item.title for entry in groups[0]["rows"][0]["items"]],
            [
                "Home Library Tracked Movie",
                "Home Library Untracked Movie",
            ],
        )

    def test_home_progress_filter_excludes_collected_untracked_items(self):
        self._set_enabled_media_types(MediaTypes.TV.value)

        tracked_item = Item.objects.create(
            title="Home Progress Tracked TV",
            media_id="home-progress-tracked-tv",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/home-progress-tracked.jpg",
        )
        tracked_tv = TV.objects.create(
            item=tracked_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        tracked_season_item = Item.objects.create(
            media_id=tracked_item.media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Home Progress Tracked TV Season 1",
            image="https://example.com/home-progress-season.jpg",
            season_number=1,
        )
        tracked_season = Season.objects.create(
            item=tracked_season_item,
            user=self.user,
            related_tv=tracked_tv,
            status=Status.IN_PROGRESS.value,
        )
        for episode_number in range(1, 4):
            episode_item = Item.objects.create(
                media_id=tracked_item.media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Home Progress Tracked TV Episode {episode_number}",
                image="https://example.com/home-progress-episode.jpg",
                season_number=1,
                episode_number=episode_number,
                release_datetime=timezone.now() - timedelta(days=episode_number),
            )
            if episode_number == 1:
                Episode.objects.create(
                    item=episode_item,
                    related_season=tracked_season,
                    end_date=timezone.now() - timedelta(days=episode_number),
                )

        Item.objects.create(
            title="Home Progress Untracked TV",
            media_id="home-progress-untracked-tv",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/home-progress-untracked.jpg",
        )
        untracked_episode = Item.objects.create(
            media_id="home-progress-untracked-tv",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Home Progress Untracked TV Episode 1",
            image="https://example.com/home-progress-untracked-ep.jpg",
            season_number=1,
            episode_number=1,
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=untracked_episode,
            media_type="digital",
        )

        HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.TV.value,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=MediaSortChoices.TITLE,
            direction=DirectionChoices.ASC,
            filters={"status": "all", "progress": "not_caught_up"},
        )

        groups = home_screen.build_home_page_groups(self.user, items_limit=10)

        self.assertEqual(
            [entry.item.title for entry in groups[0]["rows"][0]["items"]],
            ["Home Progress Tracked TV"],
        )

    def test_home_screen_settings_do_not_expose_no_status_option(self):
        self._set_enabled_media_types(MediaTypes.MOVIE.value)

        response = self.client.get(reverse("home_screen"))

        self.assertEqual(response.status_code, 200)
        sections = json.loads(response.context["home_screen_sections_json"])
        movie_section = next(
            section
            for section in sections
            if section["media_type"] == MediaTypes.MOVIE.value
        )
        status_field = next(
            field
            for field in movie_section["filter_fields"]
            if field["key"] == "status"
        )
        self.assertNotIn(
            "no_status",
            [option["value"] for option in status_field["options"]],
        )

    def test_home_filter_fields_include_collected_only_untracked_authors(self):
        self._set_enabled_media_types(MediaTypes.BOOK.value)
        untracked_book = Item.objects.create(
            title="Home Untracked Author Book",
            media_id="home-untracked-author-book",
            media_type=MediaTypes.BOOK.value,
            source=Sources.OPENLIBRARY.value,
            image="https://example.com/home-untracked-author-book.jpg",
            authors=["Author Only In Collection"],
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=untracked_book,
            media_type="audiobook",
        )

        filter_fields = home_screen.build_filter_field_data(
            self.user,
            MediaTypes.BOOK.value,
        )
        author_field = next(field for field in filter_fields if field["key"] == "author")
        self.assertIn(
            {"value": "Author Only In Collection", "label": "Author Only In Collection"},
            author_field["options"],
        )

    def test_planning_library_row_excludes_duplicate_item_with_newer_in_progress_status(self):
        self._set_enabled_media_types(MediaTypes.GAME.value)
        stale_planning_item = Item.objects.create(
            title="Multi-Session Game",
            media_id="multi-session-game",
            media_type=MediaTypes.GAME.value,
            source=Sources.IGDB.value,
            image="https://example.com/game.jpg",
        )
        visible_planning_item = Item.objects.create(
            title="Planning Only Game",
            media_id="planning-only-game",
            media_type=MediaTypes.GAME.value,
            source=Sources.IGDB.value,
            image="https://example.com/planning-game.jpg",
        )
        planning_game = Game.objects.create(
            item=stale_planning_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        in_progress_game = Game.objects.create(
            item=stale_planning_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=30,
        )
        visible_planning_game = Game.objects.create(
            item=visible_planning_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        now = timezone.now()
        Game.objects.filter(id=planning_game.id).update(
            created_at=now - timedelta(days=1),
        )
        Game.objects.filter(id=in_progress_game.id).update(created_at=now)
        Game.objects.filter(id=visible_planning_game.id).update(created_at=now - timedelta(days=2))

        HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.GAME.value,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=MediaSortChoices.TITLE,
            direction=DirectionChoices.ASC,
            filters={"status": Status.PLANNING.value},
        )

        groups = home_screen.build_home_page_groups(self.user, items_limit=10)

        row_items = groups[0]["rows"][0]["items"]
        self.assertEqual(len(row_items), 1)
        self.assertEqual(row_items[0].item.id, visible_planning_item.id)
        self.assertEqual(row_items[0].media.id, visible_planning_game.id)
        self.assertEqual(row_items[0].media.status, Status.PLANNING.value)
        self.assertEqual(
            getattr(row_items[0].media, "aggregated_status", row_items[0].media.status),
            Status.PLANNING.value,
        )

    def test_home_screen_get_seeds_default_rows_for_show_libraries(self):
        self._set_enabled_media_types(
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
        )

        response = self.client.get(reverse("home_screen"))

        sections = {
            section["media_type"]: section
            for section in json.loads(response.context["home_screen_sections_json"])
        }
        self.assertEqual(len(sections[MediaTypes.TV.value]["rows"]), 1)
        self.assertEqual(len(sections[MediaTypes.ANIME.value]["rows"]), 1)
        self.assertEqual(len(sections[MediaTypes.SEASON.value]["rows"]), 1)
        self.assertEqual(sections[MediaTypes.SEASON.value]["rows"][0]["sort_by"], HomeSortChoices.UPCOMING)
        self.assertEqual(len(sections[MediaTypes.MOVIE.value]["rows"]), 1)
        self.assertEqual(sections[MediaTypes.MOVIE.value]["rows"][0]["sort_by"], HomeSortChoices.RECENT)
        self.assertEqual(
            sections[MediaTypes.TV.value]["rows"][0]["sort_by"],
            MediaSortChoices.NEXT_EPISODE_AIR_DATE,
        )
        self.assertEqual(
            sections[MediaTypes.TV.value]["rows"][0]["direction"],
            DirectionChoices.DESC,
        )
        self.assertEqual(
            sections[MediaTypes.TV.value]["rows"][0]["filters"]["status"],
            Status.IN_PROGRESS.value,
        )
        self.assertEqual(
            sections[MediaTypes.TV.value]["rows"][0]["filters"]["progress"],
            "not_caught_up",
        )
        self.assertEqual(
            sections[MediaTypes.TV.value]["rows"][0]["title"],
            "In Progress • Not Caught Up",
        )
        self.assertEqual(
            sections[MediaTypes.TV.value]["rows"][0]["summary"],
            "Sorted by Episode Air Date • Descending",
        )
        self.assertEqual(
            sections[MediaTypes.ANIME.value]["rows"][0]["sort_by"],
            MediaSortChoices.NEXT_EPISODE_AIR_DATE,
        )
        self.assertEqual(
            sections[MediaTypes.ANIME.value]["rows"][0]["direction"],
            DirectionChoices.DESC,
        )
        self.assertEqual(
            sections[MediaTypes.ANIME.value]["rows"][0]["filters"]["progress"],
            "not_caught_up",
        )
        self.assertIn(
            {
                "value": MediaSortChoices.NEXT_EPISODE_AIR_DATE,
                "label": "Episode Air Date",
            },
            sections[MediaTypes.TV.value]["sort_choices"][HomeScreenRowTypeChoices.LIBRARY_QUERY],
        )
        self.assertFalse(
            HomeScreenRow.objects.filter(
                user=self.user,
                row_type=HomeScreenRowTypeChoices.RECENTLY_UNRATED,
            ).exists(),
        )

    def test_home_screen_get_upgrades_legacy_seeded_defaults(self):
        self._set_enabled_media_types(
            MediaTypes.SEASON.value,
            MediaTypes.MOVIE.value,
        )
        default_filters = {
            "status": Status.IN_PROGRESS.value,
            "rating": "all",
            "collection": "all",
            "genre": "",
            "year": "",
            "release": "all",
            "source": "",
            "language": "",
            "country": "",
            "platform": "",
            "origin": "",
            "format": "",
            "author": "",
            "tag": "",
            "tag_exclude": "",
        }
        for position, row_type in enumerate(
            [
                HomeScreenRowTypeChoices.LIBRARY_QUERY,
                HomeScreenRowTypeChoices.RECENTLY_UNRATED,
            ],
        ):
            HomeScreenRow.objects.create(
                user=self.user,
                media_type=MediaTypes.SEASON.value,
                position=position,
                enabled=True,
                row_type=row_type,
                sort_by=HomeSortChoices.UPCOMING if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else HomeSortChoices.RECENT,
                direction="asc" if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else "desc",
                filters=default_filters if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else {},
            )
            HomeScreenRow.objects.create(
                user=self.user,
                media_type=MediaTypes.MOVIE.value,
                position=position,
                enabled=True,
                row_type=row_type,
                sort_by=HomeSortChoices.UPCOMING if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else HomeSortChoices.RECENT,
                direction="asc" if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else "desc",
                filters=default_filters if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else {},
            )

        response = self.client.get(reverse("home_screen"))

        self.assertEqual(response.status_code, 200)
        rows = list(
            HomeScreenRow.objects.filter(user=self.user).order_by("media_type", "position", "id"),
        )
        self.assertEqual(
            [(row.media_type, row.row_type, row.sort_by) for row in rows],
            [
                (MediaTypes.MOVIE.value, HomeScreenRowTypeChoices.LIBRARY_QUERY, HomeSortChoices.RECENT),
                (MediaTypes.SEASON.value, HomeScreenRowTypeChoices.LIBRARY_QUERY, HomeSortChoices.UPCOMING),
            ],
        )

    def test_home_screen_get_upgrades_legacy_tv_and_anime_defaults(self):
        self._set_enabled_media_types(
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
        )
        default_filters = {
            "status": Status.IN_PROGRESS.value,
            "rating": "all",
            "collection": "all",
            "genre": "",
            "year": "",
            "release": "all",
            "source": "",
            "language": "",
            "country": "",
            "platform": "",
            "origin": "",
            "format": "",
            "author": "",
            "tag": "",
            "tag_exclude": "",
        }
        for media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            HomeScreenRow.objects.create(
                user=self.user,
                media_type=media_type,
                position=0,
                enabled=True,
                row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
                sort_by=MediaSortChoices.TITLE,
                direction="asc",
                filters=default_filters,
            )
            HomeScreenRow.objects.create(
                user=self.user,
                media_type=media_type,
                position=1,
                enabled=True,
                row_type=HomeScreenRowTypeChoices.RECENTLY_UNRATED,
                sort_by=HomeSortChoices.RECENT,
                direction="desc",
                filters={},
            )

        response = self.client.get(reverse("home_screen"))

        self.assertEqual(response.status_code, 200)
        tv_row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.TV.value,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
        )
        self.assertEqual(tv_row.sort_by, MediaSortChoices.NEXT_EPISODE_AIR_DATE)
        self.assertEqual(tv_row.direction, DirectionChoices.DESC)
        self.assertEqual(tv_row.filters["status"], Status.IN_PROGRESS.value)
        self.assertEqual(tv_row.filters["progress"], "not_caught_up")

        anime_row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.ANIME.value,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
        )
        self.assertEqual(anime_row.sort_by, MediaSortChoices.NEXT_EPISODE_AIR_DATE)
        self.assertEqual(anime_row.direction, DirectionChoices.DESC)
        self.assertEqual(anime_row.filters["status"], Status.IN_PROGRESS.value)
        self.assertEqual(anime_row.filters["progress"], "not_caught_up")

    def test_home_screen_get_upgrades_single_row_legacy_tv_and_anime_defaults(self):
        self._set_enabled_media_types(
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
        )
        default_filters = {
            "status": Status.IN_PROGRESS.value,
            "rating": "all",
            "collection": "all",
            "genre": "",
            "year": "",
            "release": "all",
            "source": "",
            "language": "",
            "country": "",
            "platform": "",
            "origin": "",
            "format": "",
            "author": "",
            "tag": "",
            "tag_exclude": "",
        }
        for media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            HomeScreenRow.objects.create(
                user=self.user,
                media_type=media_type,
                position=0,
                enabled=True,
                row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
                sort_by=MediaSortChoices.TITLE,
                direction=DirectionChoices.DESC,
                filters=default_filters,
            )

        response = self.client.get(reverse("home_screen"))

        self.assertEqual(response.status_code, 200)
        for media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            row = HomeScreenRow.objects.get(
                user=self.user,
                media_type=media_type,
                row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            )
            self.assertEqual(row.sort_by, MediaSortChoices.NEXT_EPISODE_AIR_DATE)
            self.assertEqual(row.direction, DirectionChoices.DESC)
            self.assertEqual(row.filters["status"], Status.IN_PROGRESS.value)
            self.assertEqual(row.filters["progress"], "not_caught_up")

    def test_home_screen_get_upgrades_original_single_row_seeded_anime_defaults(self):
        self._set_enabled_media_types(MediaTypes.ANIME.value)

        HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.ANIME.value,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=HomeSortChoices.RECENT,
            direction=DirectionChoices.DESC,
            filters={
                "status": Status.IN_PROGRESS.value,
                "rating": "all",
                "collection": "all",
                "genre": "",
                "year": "",
                "release": "all",
                "source": "",
                "language": "",
                "country": "",
                "platform": "",
                "origin": "",
                "format": "",
                "author": "",
                "tag": "",
                "tag_exclude": "",
            },
        )

        response = self.client.get(reverse("home_screen"))

        self.assertEqual(response.status_code, 200)
        anime_row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.ANIME.value,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
        )
        self.assertEqual(anime_row.sort_by, MediaSortChoices.NEXT_EPISODE_AIR_DATE)
        self.assertEqual(anime_row.direction, DirectionChoices.DESC)
        self.assertEqual(anime_row.filters["status"], Status.IN_PROGRESS.value)
        self.assertEqual(anime_row.filters["progress"], "not_caught_up")

    def test_describe_library_query_uses_static_summary_labels(self):
        """Home row summaries should not rebuild full filter-field option data."""
        filters = {
            "status": "all",
            "rating": "rated",
            "year": "unknown",
            "source": "tmdb",
            "tag_exclude": "rewatch",
        }

        with patch(
            "users.home_screen.build_filter_field_data",
            side_effect=AssertionError("summary labels should not build filter fields"),
        ):
            summary = home_screen.describe_library_query(
                filters,
                self.user,
                MediaTypes.MOVIE.value,
            )

        self.assertEqual(
            summary,
            "Library • Rated • Unknown Year • The Movie Database",
        )

    def test_home_screen_post_persists_row_configuration(self):
        self._set_enabled_media_types(MediaTypes.MOVIE.value)
        custom_list = CustomList.objects.create(name="Friday Night", owner=self.user)

        payload = [
            {
                "media_type": MediaTypes.MOVIE.value,
                "rows": [
                    {
                        "enabled": True,
                        "row_type": HomeScreenRowTypeChoices.LIBRARY_QUERY,
                        "sort_by": "title",
                        "direction": "asc",
                        "filters": {
                            "status": Status.COMPLETED.value,
                            "rating": "rated",
                            "tag": "favorite",
                        },
                    },
                    {
                        "enabled": False,
                        "row_type": HomeScreenRowTypeChoices.CUSTOM_LIST,
                        "custom_list_id": custom_list.id,
                        "sort_by": "date_added",
                        "direction": "desc",
                        "filters": {},
                    },
                    {
                        "enabled": True,
                        "row_type": HomeScreenRowTypeChoices.RECENTLY_UNRATED,
                    },
                ],
            },
        ]

        response = self.client.post(
            reverse("home_screen"),
            {"home_screen_sections": json.dumps(payload)},
        )

        self.assertRedirects(response, reverse("home_screen"))

        rows = list(
            HomeScreenRow.objects.filter(
                user=self.user,
                media_type=MediaTypes.MOVIE.value,
            ).order_by("position", "id"),
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual([row.row_type for row in rows], [
            HomeScreenRowTypeChoices.LIBRARY_QUERY,
            HomeScreenRowTypeChoices.CUSTOM_LIST,
            HomeScreenRowTypeChoices.RECENTLY_UNRATED,
        ])
        self.assertEqual(rows[0].sort_by, "title")
        self.assertEqual(rows[0].direction, "asc")
        self.assertEqual(rows[0].filters["status"], Status.COMPLETED.value)
        self.assertEqual(rows[0].filters["rating"], "rated")
        self.assertEqual(rows[0].filters["tag"], "favorite")
        self.assertFalse(rows[1].enabled)
        self.assertEqual(rows[1].custom_list_id, custom_list.id)
        self.assertEqual(rows[1].sort_by, "date_added")
        self.assertEqual(rows[2].sort_by, HomeSortChoices.RECENT)

    def test_home_screen_row_direction_toggle_persists_to_settings(self):
        self._set_enabled_media_types(MediaTypes.SEASON.value)

        self.client.get(reverse("home_screen"))
        row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.SEASON.value,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            position=0,
        )
        self.assertEqual(row.direction, DirectionChoices.ASC)

        response = self.client.post(
            reverse("toggle_home_screen_row_direction", args=[row.id]),
        )

        self.assertRedirects(response, reverse("home"))
        row.refresh_from_db()
        self.assertEqual(row.direction, DirectionChoices.DESC)

        settings_response = self.client.get(reverse("home_screen"))
        sections = {
            section["media_type"]: section
            for section in json.loads(settings_response.context["home_screen_sections_json"])
        }
        self.assertEqual(
            sections[MediaTypes.SEASON.value]["rows"][0]["direction"],
            DirectionChoices.DESC,
        )

    def test_home_screen_post_rejects_unsupported_filter_for_media_type(self):
        self._set_enabled_media_types(MediaTypes.MOVIE.value)
        self.client.get(reverse("home_screen"))
        existing_rows = HomeScreenRow.objects.filter(user=self.user).count()

        payload = [
            {
                "media_type": MediaTypes.MOVIE.value,
                "rows": [
                    {
                        "enabled": True,
                        "row_type": HomeScreenRowTypeChoices.LIBRARY_QUERY,
                        "sort_by": "title",
                        "direction": "asc",
                        "filters": {"platform": "Steam"},
                    },
                ],
            },
        ]

        response = self.client.post(
            reverse("home_screen"),
            {"home_screen_sections": json.dumps(payload)},
        )

        self.assertRedirects(response, reverse("home_screen"))
        self.assertEqual(
            HomeScreenRow.objects.filter(user=self.user).count(),
            existing_rows,
        )
        messages = list(get_messages(response.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertIn("not available for movie", str(messages[0]))

    def test_home_screen_post_rejects_inaccessible_list_reference(self):
        self._set_enabled_media_types(MediaTypes.MOVIE.value)
        other_user = get_user_model().objects.create_user(
            username="other",
            password="secret123",
        )
        other_list = CustomList.objects.create(name="Private Movies", owner=other_user)

        payload = [
            {
                "media_type": MediaTypes.MOVIE.value,
                "rows": [
                    {
                        "enabled": True,
                        "row_type": HomeScreenRowTypeChoices.CUSTOM_LIST,
                        "custom_list_id": other_list.id,
                        "sort_by": "title",
                        "direction": "asc",
                        "filters": {},
                    },
                ],
            },
        ]

        response = self.client.post(
            reverse("home_screen"),
            {"home_screen_sections": json.dumps(payload)},
        )

        self.assertRedirects(response, reverse("home_screen"))
        messages = list(get_messages(response.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertIn("Choose an accessible list", str(messages[0]))

    def test_home_screen_list_search_only_returns_accessible_lists(self):
        self._set_enabled_media_types(MediaTypes.MOVIE.value)
        owned = CustomList.objects.create(name="Weekend Watch", owner=self.user)
        smart = CustomList.objects.create(
            name="Weekend Smart",
            owner=self.user,
            is_smart=True,
            smart_media_types=[MediaTypes.MOVIE.value],
        )
        other_user = get_user_model().objects.create_user(
            username="hidden",
            password="secret123",
        )
        CustomList.objects.create(name="Weekend Hidden", owner=other_user)

        response = self.client.get(
            reverse("home_screen_list_search"),
            {"q": "Weekend", "media_type": MediaTypes.MOVIE.value},
        )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        returned_ids = {result["id"] for result in results}

        self.assertIn(owned.id, returned_ids)
        self.assertIn(smart.id, returned_ids)
        self.assertTrue(next(result for result in results if result["id"] == smart.id)["is_smart"])
        self.assertEqual(len(returned_ids), 2)
