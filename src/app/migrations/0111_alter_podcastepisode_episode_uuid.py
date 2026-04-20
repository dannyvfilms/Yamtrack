from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0110_item_manual_metadata'),
    ]

    operations = [
        migrations.AlterField(
            model_name='podcastepisode',
            name='episode_uuid',
            field=models.CharField(
                max_length=500,
                unique=True,
                help_text='Pocket Casts episode UUID or RSS GUID',
            ),
        ),
    ]
