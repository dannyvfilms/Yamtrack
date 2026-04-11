from django.contrib.auth import get_user_model
from django.test import TestCase

from lists.forms import CustomListForm
from lists.models import CustomList


class CustomListFormTest(TestCase):
    """Test the Custom List form."""

    def setUp(self):
        """Create a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def test_custom_list_form_valid(self):
        """Test the form with valid data."""
        form_data = {
            "name": "Test List",
            "description": "Test Description",
        }
        form = CustomListForm(data=form_data)
        self.assertTrue(form.is_valid())

    def test_custom_list_form_invalid(self):
        """Test the form with invalid data."""
        form_data = {
            "name": "",  # Name is required
            "description": "Test Description",
        }
        form = CustomListForm(data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_custom_list_form_with_collaborators(self):
        """Test the form with collaborators."""
        self.credentials = {"username": "test2", "password": "12345"}
        collaborator = get_user_model().objects.create_user(**self.credentials)
        form_data = {
            "name": "Test List",
            "description": "Test Description",
            "collaborators": [collaborator.id],
        }
        form = CustomListForm(data=form_data)
        self.assertTrue(form.is_valid())

    def test_custom_list_form_tags_normalized(self):
        """Ensure tags are normalized and deduplicated."""
        form_data = {
            "name": "Test List",
            "tags": ["  Sci   Fi  ", "sci fi", "Drama", ""],
        }
        form = CustomListForm(data=form_data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["tags"], ["Sci Fi", "Drama"])

    def test_custom_list_form_public_slug_normalized(self):
        """Public slug input should normalize into a URL-safe slug."""
        form = CustomListForm(
            data={
                "name": "Test List",
                "is_public": "on",
                "public_slug": "  Favorite Movies 2026!  ",
            },
            user=self.user,
        )

        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["public_slug"], "favorite-movies-2026")

    def test_custom_list_form_rejects_duplicate_public_slug(self):
        """Public slugs must be unique once claimed."""
        CustomList.objects.create(
            name="Existing",
            owner=self.user,
            visibility="public",
            public_slug="favorite-movies",
        )
        form = CustomListForm(
            data={
                "name": "Test List",
                "is_public": "on",
                "public_slug": "Favorite Movies",
            },
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("public_slug", form.errors)

    def test_custom_list_form_rejects_numeric_public_slug(self):
        """Numeric-only slugs would collide with ID-based list URLs."""
        form = CustomListForm(
            data={
                "name": "Test List",
                "is_public": "on",
                "public_slug": "12345",
            },
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("public_slug", form.errors)

    def test_custom_list_form_smart_toggle(self):
        """Smart toggle should persist list type."""
        form_data = {
            "name": "Smart List",
            "is_smart": "on",
        }
        form = CustomListForm(data=form_data, user=self.user)
        self.assertTrue(form.is_valid())

        custom_list = form.save(commit=False)
        custom_list.owner = self.user
        custom_list.save()
        form.save_m2m()

        self.assertTrue(custom_list.is_smart)
        self.assertEqual(custom_list.smart_media_types, [])
        self.assertEqual(custom_list.smart_excluded_media_types, [])
        self.assertEqual(custom_list.smart_filters, {})

    def test_custom_list_form_clears_smart_fields_when_disabled(self):
        """Disabling smart mode should clear saved smart rule data."""
        custom_list = CustomList.objects.create(
            name="Smart",
            owner=self.user,
            is_smart=True,
            smart_media_types=["movie"],
            smart_excluded_media_types=["tv"],
            smart_filters={"status": "Completed", "rating": "rated"},
        )
        form = CustomListForm(
            data={
                "name": "Smart",
                "description": "",
            },
            instance=custom_list,
            user=self.user,
        )
        self.assertTrue(form.is_valid())
        saved_list = form.save()

        self.assertFalse(saved_list.is_smart)
        self.assertEqual(saved_list.smart_media_types, [])
        self.assertEqual(saved_list.smart_excluded_media_types, [])
        self.assertEqual(saved_list.smart_filters, {})
