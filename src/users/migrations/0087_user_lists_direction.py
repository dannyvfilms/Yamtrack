from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0086_list_sort_last_watched"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="lists_direction",
            field=models.CharField(
                choices=[("asc", "Ascending"), ("desc", "Descending")],
                default="desc",
                max_length=4,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(("lists_direction__in", ["asc", "desc"])),
                name="lists_direction_valid",
            ),
        ),
    ]
