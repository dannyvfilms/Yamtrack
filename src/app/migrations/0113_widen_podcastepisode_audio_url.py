from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0112_alter_item_media_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='podcastepisode',
            name='audio_url',
            field=models.URLField(max_length=500, blank=True, default=''),
        ),
    ]
