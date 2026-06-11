from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0122_album_implied_genres"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="implied_genres",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
