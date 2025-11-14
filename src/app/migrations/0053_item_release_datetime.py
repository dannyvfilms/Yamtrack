from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0052_add_runtime_minutes_field"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="release_datetime",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

