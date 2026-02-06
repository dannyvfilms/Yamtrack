from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0079_reset_subset_episode_cast_credits"),
    ]

    operations = [
        migrations.AddField(
            model_name="metadatabackfillstate",
            name="strategy_version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
