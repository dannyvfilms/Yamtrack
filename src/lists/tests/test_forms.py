from django.contrib.auth import get_user_model
from django.test import TestCase

from lists.forms import CustomListForm


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
