import logging

from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demodemo"  # noqa: S105
DEMO_EMAIL = "demo@example.com"


def ensure_demo_user():
    """Create or normalize the built-in demo account."""
    user_model = get_user_model()
    user, created = user_model.objects.get_or_create(
        username=DEMO_USERNAME,
        defaults={
            "email": DEMO_EMAIL,
            "is_active": True,
            "is_demo": True,
            "is_staff": False,
            "is_superuser": False,
        },
    )

    update_fields = []
    field_updates = {
        "email": DEMO_EMAIL,
        "is_active": True,
        "is_demo": True,
        "is_staff": False,
        "is_superuser": False,
    }

    for field_name, value in field_updates.items():
        if getattr(user, field_name) != value:
            setattr(user, field_name, value)
            update_fields.append(field_name)

    if not user.check_password(DEMO_PASSWORD):
        user.set_password(DEMO_PASSWORD)
        update_fields.append("password")

    if update_fields:
        user.save(update_fields=update_fields)

    logger.info(
        "Demo account %s %s",
        DEMO_USERNAME,
        "created" if created else "ensured",
    )
    return user
