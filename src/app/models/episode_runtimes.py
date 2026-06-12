"""Bulk aggregation helpers for episode runtimes.

Media.total_runtime_minutes for TV/anime needs per-season episode runtimes.
Computing it per media object used to issue one query per season; these
helpers fetch all episode runtimes for a set of shows in a single query so
list pages and home rows can aggregate purely in Python.
"""

import logging
from collections import defaultdict

from app.models.choices import MediaTypes
from app.models.item import Item

logger = logging.getLogger(__name__)

# Sentinel values meaning "runtime unknown"; mirrored from the per-season
# queries this module replaces.
EXCLUDED_RUNTIME_SENTINELS = (999998, 999999)


def build_episode_runtime_index(show_keys):
    """Fetch episode runtimes for the given (media_id, source) shows in one query.

    Returns {(media_id, source): {season_number: [(episode_number, runtime), ...]}}.
    Like _annotate_tv_released_episodes, the query tolerates a superset cross
    product of ids and sources; extra rows are filtered out by the dict keying.
    """
    if not show_keys:
        return {}

    media_ids = {media_id for media_id, _ in show_keys}
    sources = {source for _, source in show_keys}
    rows = (
        Item.objects.filter(
            media_type=MediaTypes.EPISODE.value,
            media_id__in=media_ids,
            source__in=sources,
            runtime_minutes__isnull=False,
        )
        .exclude(runtime_minutes__in=EXCLUDED_RUNTIME_SENTINELS)
        .values_list(
            "media_id",
            "source",
            "season_number",
            "episode_number",
            "runtime_minutes",
        )
    )

    index = {}
    for media_id, source, season_number, episode_number, runtime in rows:
        key = (media_id, source)
        if key not in show_keys:
            continue
        index.setdefault(key, defaultdict(list))[season_number].append(
            (episode_number, runtime),
        )
    return index


def prefill_episode_runtime_index(media_list):
    """Prefill episode runtimes for TV/anime entries with a single query.

    Sets media._episode_runtime_index on every TV/anime entry (an empty dict
    counts as prefilled) so total_runtime_minutes never queries per season.
    Entries that already carry an index are left untouched.
    """
    targets = []
    show_keys = set()
    for media in media_list:
        item = getattr(media, "item", None)
        if item is None or getattr(media, "_episode_runtime_index", None) is not None:
            continue
        if item.media_type not in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            continue
        targets.append(media)
        show_keys.add((item.media_id, item.source))

    if not targets:
        return

    index = build_episode_runtime_index(show_keys)
    for media in targets:
        key = (media.item.media_id, media.item.source)
        media._episode_runtime_index = index.get(key, {})  # noqa: SLF001
