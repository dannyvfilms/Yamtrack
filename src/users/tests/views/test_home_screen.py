import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from app.models import MediaTypes, Status
from lists.models import CustomList
from users import home_screen
from users.models import HomeScreenRow, HomeScreenRowTypeChoices, HomeSortChoices


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

    def test_home_screen_get_seeds_old_home_style_defaults(self):
        self._set_enabled_media_types(
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
            MediaTypes.MOVIE.value,
        )

        response = self.client.get(reverse("home_screen"))

        sections = {
            section["media_type"]: section
            for section in json.loads(response.context["home_screen_sections_json"])
        }
        self.assertEqual(sections[MediaTypes.TV.value]["rows"], [])
        self.assertEqual(len(sections[MediaTypes.SEASON.value]["rows"]), 1)
        self.assertEqual(sections[MediaTypes.SEASON.value]["rows"][0]["sort_by"], HomeSortChoices.UPCOMING)
        self.assertEqual(len(sections[MediaTypes.MOVIE.value]["rows"]), 1)
        self.assertEqual(sections[MediaTypes.MOVIE.value]["rows"][0]["sort_by"], HomeSortChoices.RECENT)
        self.assertFalse(
            HomeScreenRow.objects.filter(
                user=self.user,
                row_type=HomeScreenRowTypeChoices.RECENTLY_UNRATED,
            ).exists(),
        )

    def test_home_screen_get_upgrades_legacy_seeded_defaults(self):
        self._set_enabled_media_types(
            MediaTypes.TV.value,
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
                media_type=MediaTypes.TV.value,
                position=position,
                enabled=True,
                row_type=row_type,
                sort_by=HomeSortChoices.UPCOMING if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else HomeSortChoices.RECENT,
                direction="asc" if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else "desc",
                filters=default_filters if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY else {},
            )
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
