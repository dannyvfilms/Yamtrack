# Generated manually on 2026-02-15

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0089_set_null_themes_to_empty_list"),
    ]

    operations = [
        migrations.AlterField(
            model_name="item",
            name="themes",
            field=models.JSONField(
                blank=True, default=list, help_text="Array of themes (Games)"
            ),
        ),
    ]
