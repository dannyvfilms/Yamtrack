from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0092_rename_next_episode_air_date_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="session_duration",
            field=models.IntegerField(
                choices=[
                    (86400, "1 day"),
                    (604800, "1 week"),
                    (1209600, "2 weeks"),
                    (2592000, "30 days"),
                    (7776000, "90 days"),
                ],
                default=1209600,
                help_text="How long a login session persists before requiring re-authentication",
            ),
        ),
    ]
