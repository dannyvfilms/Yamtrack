from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.utils import OperationalError, ProgrammingError
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from users.demo import ensure_demo_user


def _demo_user_schema_ready():
    """Only provision the demo user once the current user schema exists."""
    user_model = get_user_model()
    table_name = user_model._meta.db_table

    try:
        if table_name not in connection.introspection.table_names():
            return False

        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor,
                    table_name,
                )
            }
    except (OperationalError, ProgrammingError):
        return False

    required_columns = {
        field.column
        for field in user_model._meta.local_concrete_fields
    }
    return required_columns.issubset(columns)


@receiver(post_migrate)
def ensure_demo_user_after_migrate(sender, **kwargs):  # noqa: ARG001
    """Provision the configured demo account after migrations."""
    if getattr(settings, "TESTING", False):
        return

    if getattr(sender, "label", None) != "users":
        return

    if not _demo_user_schema_ready():
        return

    ensure_demo_user()
