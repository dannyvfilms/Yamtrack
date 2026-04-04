from django.contrib.auth import get_user_model
from django.test import TestCase

from users.demo import DEMO_EMAIL, DEMO_PASSWORD, DEMO_USERNAME, ensure_demo_user


class EnsureDemoUserTests(TestCase):
    """Tests for built-in demo account provisioning."""

    changed_password = "changed-password"  # noqa: S105

    def test_creates_demo_user_with_expected_credentials(self):
        """Demo provisioning should create the built-in account."""
        user = ensure_demo_user()

        self.assertIsNotNone(user)
        self.assertEqual(user.username, DEMO_USERNAME)
        self.assertTrue(user.is_demo)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.email, DEMO_EMAIL)
        self.assertTrue(user.check_password(DEMO_PASSWORD))

    def test_normalizes_existing_demo_username(self):
        """Provisioning should reset a public demo account to the built-in state."""
        user = get_user_model().objects.create_user(
            username=DEMO_USERNAME,
            password=self.changed_password,
            email="wrong@example.com",
        )
        user.is_demo = False
        user.is_active = False
        user.is_staff = True
        user.is_superuser = True
        user.save(
            update_fields=[
                "is_demo",
                "is_active",
                "is_staff",
                "is_superuser",
            ],
        )

        ensure_demo_user()

        user.refresh_from_db()
        self.assertTrue(user.is_demo)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.email, DEMO_EMAIL)
        self.assertTrue(user.check_password(DEMO_PASSWORD))
