# Generated manually on 2026-02-15

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0087_alter_metadatabackfillstate_field"),
    ]

    operations = [
        migrations.AlterField(
            model_name="item",
            name="themes",
            field=models.JSONField(
                blank=True, default=list, help_text="Array of themes (Games)", null=True
            ),
        ),
    ]
