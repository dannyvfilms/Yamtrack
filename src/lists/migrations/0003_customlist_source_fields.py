from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lists", "0002_alter_customlistitem_item_alter_customlist_items_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customlist",
            name="source",
            field=models.CharField(
                choices=[("local", "Local"), ("trakt", "Trakt")],
                default="local",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="customlist",
            name="source_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]
