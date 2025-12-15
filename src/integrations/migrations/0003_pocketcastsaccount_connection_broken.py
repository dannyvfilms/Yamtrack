# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('integrations', '0002_alter_plexaccount_options_pocketcastsaccount'),
    ]

    operations = [
        migrations.AddField(
            model_name='pocketcastsaccount',
            name='connection_broken',
            field=models.BooleanField(
                default=False,
                help_text='True if connection is broken (refresh failed) but credentials are preserved'
            ),
        ),
    ]
