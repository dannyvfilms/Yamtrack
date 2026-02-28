from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class PasswordRecoveryViewTests(TestCase):
    """Tests for self-service password recovery flow."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="recover-me",
            password="old-pass-123",
        )
        self.recovery_code = self.user.generate_recovery_codes(count=1)[0]

    def test_recovery_page_accessible_without_login(self):
        """Password recovery page should be publicly accessible."""
        response = self.client.get(reverse("password_recover"))
        self.assertEqual(response.status_code, 200)

    def test_recover_password_with_recovery_code(self):
        """Recovery code should allow password reset for user without authenticator enabled."""
        response = self.client.post(
            reverse("password_recover"),
            {
                "username": self.user.username,
                "recovery_code": self.recovery_code,
                "new_password1": "new-secure-pass-123",
                "new_password2": "new-secure-pass-123",
            },
            follow=True,
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secure-pass-123"))
        self.assertContains(response, "Password updated")

    def test_recover_password_with_authenticator_code_when_enabled(self):
        """Authenticator code alone should allow password reset when authenticator is enabled."""
        secret = self.user.get_or_create_authenticator_secret()
        self.user.authenticator_enabled = True
        self.user.save(update_fields=["authenticator_enabled"])

        import pyotp

        code = pyotp.TOTP(secret).now()
        response = self.client.post(
            reverse("password_recover"),
            {
                "username": self.user.username,
                "authenticator_code": code,
                "new_password1": "new-secure-pass-123",
                "new_password2": "new-secure-pass-123",
            },
            follow=True,
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secure-pass-123"))
        self.assertContains(response, "Password updated")

    def test_recover_password_falls_back_to_recovery_code_when_no_authenticator_code(self):
        """Recovery code should still work as fallback when authenticator is enabled."""
        self.user.get_or_create_authenticator_secret()
        self.user.authenticator_enabled = True
        self.user.save(update_fields=["authenticator_enabled"])

        response = self.client.post(
            reverse("password_recover"),
            {
                "username": self.user.username,
                "recovery_code": self.recovery_code,
                "new_password1": "new-secure-pass-123",
                "new_password2": "new-secure-pass-123",
            },
            follow=True,
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-secure-pass-123"))
        self.assertContains(response, "Password updated")

    def test_recover_password_fails_when_enabled_and_no_authenticator_or_recovery_code(self):
        """Recovery should fail when neither authenticator code nor recovery code is provided."""
        self.user.get_or_create_authenticator_secret()
        self.user.authenticator_enabled = True
        self.user.save(update_fields=["authenticator_enabled"])

        response = self.client.post(
            reverse("password_recover"),
            {
                "username": self.user.username,
                "new_password1": "new-secure-pass-123",
                "new_password2": "new-secure-pass-123",
            },
        )

        self.assertContains(response, "Unable to verify recovery details")
