from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0100_discovertasteprofile_comfort_library_affinity_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="provider_external_ids",
            field=models.JSONField(blank=True, default=dict, help_text="Resolved external ids"),
        ),
        migrations.AddField(
            model_name="item",
            name="provider_game_lengths",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Persisted game length metadata from external providers",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="provider_game_lengths_fetched_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When game length metadata was last fetched",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="provider_game_lengths_match",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", ""),
                    ("direct_url", "Direct URL"),
                    ("exact_title_year", "Exact Title + Year"),
                    ("steam_verified", "Steam Verified"),
                    ("ambiguous", "Ambiguous"),
                    ("igdb_fallback", "IGDB Fallback"),
                ],
                default="",
                help_text="How the active game length metadata was matched",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="provider_game_lengths_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", ""),
                    ("hltb", "HowLongToBeat"),
                    ("igdb", "IGDB"),
                ],
                default="",
                help_text="Active provider for persisted game length metadata",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="metadatabackfillstate",
            name="field",
            field=models.CharField(
                choices=[
                    ("runtime", "Runtime"),
                    ("genres", "Genres"),
                    ("credits", "Credits"),
                    ("release", "Release Date"),
                    ("discover", "Discover Metadata"),
                    ("game_lengths", "Game Lengths"),
                ],
                max_length=20,
            ),
        ),
    ]
