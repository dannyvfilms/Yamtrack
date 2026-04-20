from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0111_alter_podcastepisode_episode_uuid'),
    ]

    operations = [
        migrations.AlterField(
            model_name='item',
            name='media_id',
            field=models.CharField(max_length=500),
        ),
    ]
