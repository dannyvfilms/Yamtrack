from django.contrib import auth
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class Profile(TestCase):
    """Test profile page."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_change_username(self):
        """Test changing username."""
        self.assertEqual(auth.get_user(self.client).username, "test")
        self.client.post(
            reverse("account"),
            {
                "username": "new_test",
            },
        )
        self.assertEqual(auth.get_user(self.client).username, "new_test")

    def test_change_password(self):
        """Test changing password."""
        self.assertEqual(auth.get_user(self.client).check_password("12345"), True)
        self.client.post(
            reverse("account"),
            {
                "old_password": "12345",
                "new_password1": "*FNoZN64",
                "new_password2": "*FNoZN64",
            },
        )
        self.assertEqual(auth.get_user(self.client).check_password("*FNoZN64"), True)

    def test_invalid_password_change(self):
        """Test password change with incorrect old password."""
        response = self.client.post(
            reverse("account"),
            {
                "old_password": "wrongpass",
                "new_password1": "newpass123",
                "new_password2": "newpass123",
            },
        )
        self.assertTrue(auth.get_user(self.client).check_password("12345"))
        self.assertContains(response, "Your old password was entered incorrectly")

    def test_account_page_shows_qr_setup_option(self):
        """Account page should include QR setup data for authenticator apps."""
        response = self.client.get(reverse("account"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scan this QR code")
        self.assertTrue(response.context["authenticator_uri"].startswith("otpauth://"))
        self.assertTrue(
            response.context["authenticator_qr_data_uri"].startswith("data:image/png;base64,"),
        )

    def test_account_page_shows_management_actions_when_authenticator_enabled(self):
        """Configured accounts should show authenticator management actions."""
        self.user.get_or_create_authenticator_secret()
        self.user.authenticator_enabled = True
        self.user.save(update_fields=["authenticator_enabled"])

        response = self.client.get(reverse("account"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add New Authenticator App")
        self.assertContains(response, "Deactivate Authenticator")
        self.assertFalse(response.context["show_authenticator_setup"])
        self.assertEqual(response.context["authenticator_qr_data_uri"], "")

    def test_disable_authenticator_does_not_require_password(self):
        """Configured users can deactivate authenticator directly from management actions."""
        self.user.get_or_create_authenticator_secret()
        self.user.authenticator_enabled = True
        self.user.save(update_fields=["authenticator_enabled"])

        response = self.client.post(
            reverse("account"),
            {"action": "disable_authenticator"},
            follow=True,
        )

        self.user.refresh_from_db()
        self.assertFalse(self.user.authenticator_enabled)
        self.assertContains(response, "Scan this QR code")
