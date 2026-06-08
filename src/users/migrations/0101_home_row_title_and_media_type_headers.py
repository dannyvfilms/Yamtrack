from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0100_quick_season_update_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="homescreenrow",
            name="title",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="user",
            name="home_show_media_type_headers",
            field=models.BooleanField(
                default=False,
                help_text="Show a media-type header (icon + name) above each group of home screen rows",
            ),
        ),
    ]
