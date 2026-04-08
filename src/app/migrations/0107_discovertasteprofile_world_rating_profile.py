from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0106_add_item_query_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="discovertasteprofile",
            name="world_rating_profile",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
