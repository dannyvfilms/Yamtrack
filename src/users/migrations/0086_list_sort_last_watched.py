from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("users", "0085_remove_user_tv_sort_valid_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="user",
            name="lists_sort_valid",
        ),
        migrations.AlterField(
            model_name="user",
            name="lists_sort",
            field=models.CharField(
                choices=[
                    ("last_item_added", "Last Item Added"),
                    ("last_watched", "Last Watched"),
                    ("name", "Name"),
                    ("items_count", "Items Count"),
                    ("newest_first", "Newest First"),
                ],
                default="last_item_added",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="list_detail_sort",
            field=models.CharField(
                choices=[
                    ("date_added", "Date Added"),
                    ("custom", "Custom"),
                    ("title", "Title"),
                    ("media_type", "Media Type"),
                    ("rating", "Rating"),
                    ("progress", "Progress"),
                    ("release_date", "Release Date"),
                    ("start_date", "Start Date"),
                    ("end_date", "End Date"),
                ],
                default="date_added",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "lists_sort__in",
                        [
                            "last_item_added",
                            "last_watched",
                            "name",
                            "items_count",
                            "newest_first",
                        ],
                    ),
                ),
                name="lists_sort_valid",
            ),
        ),
    ]
