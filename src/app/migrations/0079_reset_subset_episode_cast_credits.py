from collections import defaultdict

from django.db import migrations


def reset_subset_episode_cast_credits(apps, schema_editor):  # noqa: ARG001
    """Clear likely legacy TMDB episode credits when cast is a tiny show-level subset."""
    Item = apps.get_model("app", "Item")
    Episode = apps.get_model("app", "Episode")
    ItemPersonCredit = apps.get_model("app", "ItemPersonCredit")
    MetadataBackfillState = apps.get_model("app", "MetadataBackfillState")

    tmdb_source = "tmdb"
    episode_media_type = "episode"
    cast_role_type = "cast"
    credits_field = "credits"
    max_subset_size = 6

    episode_item_ids = list(
        Item.objects.filter(
            source=tmdb_source,
            media_type=episode_media_type,
        ).values_list("id", flat=True),
    )
    if not episode_item_ids:
        return

    episode_to_show = {}
    for episode_item_id, show_item_id in Episode.objects.filter(
        item_id__in=episode_item_ids,
    ).values_list("item_id", "related_season__related_tv__item_id"):
        if show_item_id:
            episode_to_show[episode_item_id] = show_item_id
    if not episode_to_show:
        return

    related_item_ids = set(episode_to_show.keys()) | set(episode_to_show.values())
    cast_person_ids_by_item = defaultdict(set)
    for item_id, person_id in ItemPersonCredit.objects.filter(
        item_id__in=related_item_ids,
        role_type=cast_role_type,
    ).values_list("item_id", "person_id"):
        cast_person_ids_by_item[item_id].add(person_id)

    stale_episode_ids = []
    for episode_item_id, show_item_id in episode_to_show.items():
        episode_cast_ids = cast_person_ids_by_item.get(episode_item_id, set())
        show_cast_ids = cast_person_ids_by_item.get(show_item_id, set())
        if not episode_cast_ids or not show_cast_ids:
            continue
        if len(episode_cast_ids) > max_subset_size:
            continue
        if episode_cast_ids.issubset(show_cast_ids):
            stale_episode_ids.append(episode_item_id)

    if not stale_episode_ids:
        return

    ItemPersonCredit.objects.filter(item_id__in=stale_episode_ids).delete()
    MetadataBackfillState.objects.filter(
        item_id__in=stale_episode_ids,
        field=credits_field,
    ).update(
        fail_count=0,
        last_attempt_at=None,
        last_success_at=None,
        next_retry_at=None,
        give_up=False,
        last_error="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0078_reset_legacy_episode_credits"),
    ]

    operations = [
        migrations.RunPython(
            reset_subset_episode_cast_credits,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
