import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from integrations.tasks._import_helpers import (
    ERROR_TITLE,
    GOODREADS_IMPORT_TASK_NAME,
    LEGACY_GOODREADS_IMPORT_TASK_NAMES,
    _coerce_uploaded_file,
    _is_expected_plex_lookup_error,
    format_import_message,
    format_media_type_display,
    format_watchlist_sync_message,
)
from integrations.tasks._webhook import (
    WEBHOOK_PROCESSORS,
    _webhook_history_user,
    process_webhook,
)
from integrations.tasks._media_imports import (
    _queue_post_import_collection_update,
    _run_arr_import,
    import_anilist,
    import_audiobookshelf,
    import_audiobookshelf_recurring,
    import_goodreads,
    import_goodreads_dotted,
    import_goodreads_legacy,
    import_gpodder,
    import_gpodder_recurring,
    import_hardcover,
    import_hltb,
    import_imdb,
    import_kitsu,
    import_mal,
    import_media,
    import_plex,
    import_pocketcasts,
    import_pocketcasts_history,
    import_radarr,
    import_radarr_recurring,
    import_simkl,
    import_sonarr,
    import_sonarr_recurring,
    import_steam,
    import_storyteller,
    import_storyteller_recurring,
    import_trakt,
    import_yamtrack,
    sync_plex_watchlist,
)
from integrations.tasks._lastfm import (
    LASTFM_PARTIAL_SYNC_ERROR,
    _enqueue_lastfm_music_enrichment,
    _refresh_lastfm_statistics,
    _run_incremental_lastfm_sync,
    import_lastfm_history,
    poll_all_lastfm_scrobbles,
    poll_lastfm_for_user,
)
from integrations.tasks._plex_collection import (
    _aggregate_tv_show_collection_metadata,
    _find_plex_rating_key_for_item,
    fetch_collection_metadata_for_item,
    update_collection_metadata_from_plex,
    update_collection_metadata_from_plex_webhook,
)

logger = logging.getLogger(__name__)


@shared_task(name="Scheduled backup export")
def scheduled_backup_export(user_id, media_types=None, include_lists=True):
    """Celery task for exporting a CSV backup to the backup directory."""
    from integrations import exports

    User = get_user_model()
    user = User.objects.get(id=user_id)
    filepath = exports.write_backup(user, media_types=media_types, include_lists=include_lists)
    return f"Backup saved to {filepath}"
