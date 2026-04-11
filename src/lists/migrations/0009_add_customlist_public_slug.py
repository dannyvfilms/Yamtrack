from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lists", "0008_customlist_smart_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="customlist",
            name="public_slug",
            field=models.SlugField(blank=True, default="", max_length=255),
        ),
        migrations.AddConstraint(
            model_name="customlist",
            constraint=models.UniqueConstraint(
                condition=~models.Q(public_slug=""),
                fields=("public_slug",),
                name="lists_customlist_public_slug_unique",
            ),
        ),
    ]
