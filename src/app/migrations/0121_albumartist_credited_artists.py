from django.db import migrations, models
import django.db.models.deletion


def backfill_album_artist_credits(apps, schema_editor):
    Album = apps.get_model("app", "Album")
    AlbumArtist = apps.get_model("app", "AlbumArtist")

    for album in (
        Album.objects.filter(artist__isnull=False)
        .select_related("artist")
        .iterator(chunk_size=500)
    ):
        AlbumArtist.objects.get_or_create(
            album=album,
            artist=album.artist,
            defaults={"position": 0, "join_phrase": ""},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0120_comicissue_historicalcomicissue_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AlbumArtist",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("position", models.PositiveIntegerField(default=0)),
                ("join_phrase", models.CharField(blank=True, default="", max_length=100)),
                (
                    "album",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="artist_credits",
                        to="app.album",
                    ),
                ),
                (
                    "artist",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="album_credits",
                        to="app.artist",
                    ),
                ),
            ],
            options={
                "ordering": ["position"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("album", "artist"),
                        name="unique_album_artist_credit",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="album",
            name="credited_artists",
            field=models.ManyToManyField(
                blank=True,
                related_name="credited_albums",
                through="app.AlbumArtist",
                to="app.artist",
            ),
        ),
        migrations.RunPython(
            backfill_album_artist_credits,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
