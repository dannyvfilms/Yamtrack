# Generated migration for Jellyfin integration settings

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0089_remove_user_tv_sort_valid_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='jellyfin_provider_priority_enabled',
            field=models.BooleanField(
                default=False,
                help_text="Prioritize your Metadata Providers preference when resolving Jellyfin webhooks",
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='jellyfin_match_existing_enabled',
            field=models.BooleanField(
                default=False,
                help_text="Try matching existing tracked items by any metadata provider first",
            ),
        ),
    ]
