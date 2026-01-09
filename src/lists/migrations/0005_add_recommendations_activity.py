import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0068_metadata_backfill_state"),
        ("lists", "0004_customlist_visibility"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="customlist",
            name="allow_recommendations",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Allow anyone to recommend items to add to this list "
                    "(only for public lists)"
                ),
            ),
        ),
        migrations.AddField(
            model_name="customlistitem",
            name="added_by",
            field=models.ForeignKey(
                blank=True,
                help_text="The user who added this item to the list",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="ListRecommendation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "anonymous_name",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Display name for anonymous recommenders",
                        max_length=100,
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text=(
                            "Optional note from the recommender explaining "
                            "their recommendation"
                        ),
                    ),
                ),
                ("date_recommended", models.DateTimeField(auto_now_add=True)),
                (
                    "custom_list",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recommendations",
                        to="lists.customlist",
                    ),
                ),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="app.item")),
                (
                    "recommended_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="The user who recommended this item (null if anonymous)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-date_recommended"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("item", "custom_list"),
                        name="lists_listrecommendation_unique_item_list",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ListActivity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "activity_type",
                    models.CharField(
                        choices=[
                            ("item_added", "Item Added"),
                            ("item_removed", "Item Removed"),
                            ("recommendation_approved", "Recommendation Approved"),
                            ("recommendation_denied", "Recommendation Denied"),
                            ("list_created", "List Created"),
                            ("list_edited", "List Edited"),
                            ("collaborator_added", "Collaborator Added"),
                            ("collaborator_removed", "Collaborator Removed"),
                        ],
                        max_length=30,
                    ),
                ),
                (
                    "details",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Additional details about the activity",
                    ),
                ),
                ("timestamp", models.DateTimeField(auto_now_add=True)),
                (
                    "custom_list",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="lists.customlist",
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        blank=True,
                        help_text="The item involved in this activity (if applicable)",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="app.item",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        help_text="The user who performed this action",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "List activities",
                "ordering": ["-timestamp"],
            },
        ),
    ]
