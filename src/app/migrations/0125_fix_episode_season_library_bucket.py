"""Clean up stray ('episode','season') Item rows.

Episodes were historically created with the season's own ``library_media_type``
('season') when a season/show was marked complete (see Season.get_episode_item
before the accompanying fix). The canonical bucket for a normal show's episode is
'episode'. This migration re-buckets each stray row to 'episode', or merges it
into the existing canonical 'episode' row (repointing dependents) when one
already exists.
"""

from django.db import migrations, transaction
from django.db.utils import IntegrityError


def fix_episode_season_buckets(apps, schema_editor):
    """Re-bucket or merge stray ('episode','season') items."""
    del schema_editor
    Item = apps.get_model("app", "Item")

    strays = Item.objects.filter(media_type="episode", library_media_type="season")

    for stray in strays.iterator():
        canonical = (
            Item.objects.filter(
                media_id=stray.media_id,
                source=stray.source,
                media_type="episode",
                library_media_type="episode",
                season_number=stray.season_number,
                episode_number=stray.episode_number,
            )
            .exclude(pk=stray.pk)
            .first()
        )

        if canonical is None:
            # No canonical row exists -> simply re-bucket in place. This cannot
            # collide: the only constraint covering this row keys on
            # library_media_type, and we just confirmed no 'episode' sibling.
            stray.library_media_type = "episode"
            stray.save(update_fields=["library_media_type"])
            continue

        # A canonical row exists -> repoint every dependent at it, then delete
        # the stray. Most strays are orphans, so the loop is usually a no-op.
        for relation in Item._meta.related_objects:
            related_model = relation.related_model
            field_name = relation.field.name
            referencing = related_model.objects.filter(**{field_name: stray})
            for obj in referencing.iterator():
                try:
                    with transaction.atomic():
                        setattr(obj, field_name, canonical)
                        obj.save(update_fields=[relation.field.attname])
                except IntegrityError:
                    # Canonical already has an equivalent row (unique clash);
                    # the stray's duplicate is redundant.
                    obj.delete()

        stray.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0124_podcastshow_source"),
    ]

    operations = [
        migrations.RunPython(
            fix_episode_season_buckets,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
