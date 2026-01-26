import logging

from django.conf import settings
from django.db import transaction

from app.models import Item, MediaTypes, Sources
from app.providers import services
from integrations.imports import helpers
from lists.models import CustomList, CustomListItem

logger = logging.getLogger(__name__)

TRAKT_API_BASE_URL = "https://api.trakt.tv"


def import_trakt_lists(user, access_token, client_id=None):
    """Import and rebuild Trakt lists for a user."""
    trakt_lists = _get_trakt_lists(access_token, client_id=client_id)
    imported_count = 0
    skipped_lists = 0
    skipped_items = 0

    with transaction.atomic():
        helpers.retry_on_lock(
            lambda: CustomList.objects.filter(owner=user, source="trakt").delete(),
        )

        for trakt_list in trakt_lists:
            list_id = trakt_list.get("ids", {}).get("trakt")
            if not list_id:
                skipped_lists += 1
                continue
            custom_list = _create_custom_list(user, trakt_list, list_id)
            imported_count += 1
            list_items = _get_trakt_list_items(access_token, list_id, client_id=client_id)
            for entry in list_items:
                item = _build_item_from_entry(entry)
                if not item:
                    skipped_items += 1
                    continue
                CustomListItem.objects.get_or_create(
                    custom_list=custom_list,
                    item=item,
                    defaults={"added_by": user},
                )

        # Import Watchlist as a special list
        try:
            logger.info("Fetching Watchlist for user %s", user.username)
            watchlist_items = _get_trakt_watchlist_items(access_token, client_id=client_id)
            logger.info(
                "Fetched %s items from Watchlist for user %s",
                len(watchlist_items) if watchlist_items else 0,
                user.username,
            )
            watchlist_list = CustomList.objects.create(
                name="Watchlist",
                description="",
                owner=user,
                visibility="private",
                allow_recommendations=False,
                source="trakt",
                source_id="watchlist",
            )
            imported_count += 1
            if watchlist_items:
                for entry in watchlist_items:
                    item = _build_item_from_entry(entry)
                    if not item:
                        skipped_items += 1
                        continue
                    CustomListItem.objects.get_or_create(
                        custom_list=watchlist_list,
                        item=item,
                        defaults={"added_by": user},
                    )
            logger.info(
                "Successfully imported Watchlist for user %s (%s items)",
                user.username,
                watchlist_list.items.count(),
            )
        except Exception as e:
            logger.warning(
                "Failed to import Watchlist for %s: %s",
                user.username,
                e,
                exc_info=True,
            )
            skipped_lists += 1

    logger.info(
        "Imported %s Trakt lists for %s (%s lists skipped, %s items skipped)",
        imported_count,
        user.username,
        skipped_lists,
        skipped_items,
    )


def _get_trakt_lists(access_token, client_id=None):
    """Fetch Trakt lists for the authenticated user."""
    return _make_trakt_request(
        access_token,
        f"{TRAKT_API_BASE_URL}/users/me/lists",
        client_id=client_id,
    )


def _get_trakt_list_items(access_token, list_id, client_id=None):
    """Fetch items for a Trakt list."""
    url = f"{TRAKT_API_BASE_URL}/users/me/lists/{list_id}/items"
    return _make_trakt_request(access_token, url, client_id=client_id)


def _get_trakt_watchlist_items(access_token, client_id=None):
    """Fetch items from the Trakt watchlist."""
    url = f"{TRAKT_API_BASE_URL}/users/me/watchlist"
    return _make_trakt_request(access_token, url, client_id=client_id)


def _make_trakt_request(access_token, url, client_id=None):
    """Make an authenticated Trakt API request."""
    if not client_id:
        client_id = settings.TRAKT_API
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "Authorization": f"Bearer {access_token}",
    }
    try:
        return services.api_request("TRAKT", "GET", url, headers=headers)
    except services.ProviderAPIError as error:
        if error.status_code == 401:
            msg = "Trakt authorization expired. Please connect again."
            raise helpers.MediaImportError(msg) from error
        raise


def _create_custom_list(user, trakt_list, list_id):
    """Create a CustomList from a Trakt list payload."""
    privacy = trakt_list.get("privacy", "private")
    visibility = "public" if privacy == "public" else "private"
    return CustomList.objects.create(
        name=trakt_list.get("name", "Trakt List"),
        description=trakt_list.get("description") or "",
        owner=user,
        visibility=visibility,
        allow_recommendations=False,
        source="trakt",
        source_id=str(list_id),
    )


def _build_item_from_entry(entry):
    """Create or fetch an Item from a Trakt list entry."""
    if entry["type"] == "movie":
        payload = entry["movie"]
        media_type = MediaTypes.MOVIE.value
    elif entry["type"] == "show":
        payload = entry["show"]
        media_type = MediaTypes.TV.value
    else:
        return None

    tmdb_id = payload.get("ids", {}).get("tmdb")
    title = payload.get("title")

    if not tmdb_id or not title:
        return None

    metadata = _get_metadata(media_type, str(tmdb_id), title)
    if not metadata:
        return None

    item, _ = Item.objects.get_or_create(
        media_id=str(tmdb_id),
        source=Sources.TMDB.value,
        media_type=media_type,
        defaults={
            "title": metadata["title"],
            "image": metadata["image"],
        },
    )
    return item


def _get_metadata(media_type, tmdb_id, title):
    """Fetch TMDB metadata for a Trakt entry."""
    try:
        return services.get_media_metadata(
            media_type,
            tmdb_id,
            Sources.TMDB.value,
        )
    except services.ProviderAPIError as error:
        if error.status_code == 404:
            logger.warning(
                "Trakt list item %s missing in TMDB (%s)",
                title,
                tmdb_id,
            )
            return None
        raise
