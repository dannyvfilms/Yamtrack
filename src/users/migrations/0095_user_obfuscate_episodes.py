from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0094_user_import_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="obfuscate_episodes",
            field=models.BooleanField(
                default=False,
                help_text="Blur unseen episode thumbnails to avoid spoilers",
            ),
        ),
    ]
