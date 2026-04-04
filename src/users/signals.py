from django.conf import settings
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from users.demo import ensure_demo_user


@receiver(post_migrate)
def ensure_demo_user_after_migrate(sender, **kwargs):  # noqa: ARG001
    """Provision the configured demo account after migrations."""
    if getattr(settings, "TESTING", False):
        return

    if getattr(sender, "label", None) != "users":
        return

    ensure_demo_user()
