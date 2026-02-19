from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lists", "0007_add_customlist_tags"),
    ]

    operations = [
        migrations.AddField(
            model_name="customlist",
            name="is_smart",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customlist",
            name="smart_excluded_media_types",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Media types excluded from this smart list.",
            ),
        ),
        migrations.AddField(
            model_name="customlist",
            name="smart_filters",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Saved filter criteria for smart lists.",
            ),
        ),
        migrations.AddField(
            model_name="customlist",
            name="smart_media_types",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Media types included in this smart list.",
            ),
        ),
    ]
