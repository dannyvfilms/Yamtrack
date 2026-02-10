from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0064_add_top_talent_sort_preference"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="table_column_prefs",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Per-library table column order and hidden keys",
            ),
        ),
    ]
