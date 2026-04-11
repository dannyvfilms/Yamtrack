from app import custom_metadata, models
from app.models import MediaTypes, Sources


def _base_response(item):
    """Return the shared provider-style response shell for a manual item."""
    return {
        "media_id": item.media_id,
        "source": Sources.MANUAL.value,
        "media_type": item.media_type,
        "title": custom_metadata.provider_value_for_item(item, "title"),
        "original_title": custom_metadata.provider_value_for_item(
            item,
            "original_title",
            fallback=item.original_title,
        ),
        "localized_title": custom_metadata.provider_value_for_item(
            item,
            "localized_title",
            fallback=item.localized_title or item.title,
        ),
        "max_progress": None,
        "image": custom_metadata.provider_value_for_item(
            item,
            custom_metadata.IMAGE_FIELD_NAME,
            fallback=item.image,
        ),
        "synopsis": custom_metadata.get_manual_synopsis(item),
        "score": None,
        "score_count": None,
        "genres": custom_metadata.provider_value_for_item(
            item,
            "genres",
            fallback=item.genres,
        ),
        "details": {},
        "related": {},
    }


def _season_title(season_item):
    """Return the displayed season title for a manual season item."""
    return (
        custom_metadata.get_manual_top_level_value(season_item, "season_title")
        or f"Season {season_item.season_number}"
    )


def _episode_title(episode_item):
    """Return the displayed episode title for a manual episode item."""
    return (
        custom_metadata.get_manual_top_level_value(episode_item, "episode_title")
        or episode_item.title
    )


def metadata(media_id, media_type):
    """Return the metadata for a manual media item."""
    item = models.Item.objects.get(
        media_id=media_id,
        media_type=media_type,
        source=Sources.MANUAL.value,
    )
    response = _base_response(item)

    fallback_details = {}
    fallback_max_progress = None

    if media_type == MediaTypes.TV.value:
        season_items = get_season_items(media_id)
        if season_items.count() > 0:
            fallback_details["seasons"] = season_items.count()

        num_episodes = process_seasons(season_items, response)
        if num_episodes:
            fallback_details["episodes"] = num_episodes
            fallback_max_progress = num_episodes
    elif media_type == MediaTypes.SEASON.value:
        season_episodes = get_season_episodes(item)
        episode_count = season_episodes.count()
        if episode_count:
            fallback_details["episodes"] = episode_count
            fallback_max_progress = episode_count
        response["episodes"] = build_episodes_response(season_episodes)
        response["season_title"] = _season_title(item)
    elif media_type == MediaTypes.MOVIE.value:
        fallback_max_progress = 1

    details = custom_metadata.build_manual_detail_payload(
        item,
        fallback_details=fallback_details,
    )
    response["details"] = details
    response["max_progress"] = custom_metadata.manual_max_progress(
        item,
        details,
        fallback_max_progress=fallback_max_progress,
    )
    return response


def season(media_id, season_number):
    """Return the metadata for a manual season."""
    tv_metadata = metadata(media_id, MediaTypes.TV.value)
    return tv_metadata[f"season/{season_number}"]


def get_season_items(media_id):
    """Get all season items for a media ID."""
    return models.Item.objects.filter(
        media_id=media_id,
        source=Sources.MANUAL.value,
        media_type=MediaTypes.SEASON.value,
    )


def process_seasons(season_items, response):
    """Process all seasons and return total episode count."""
    num_episodes = 0
    response["related"]["seasons"] = []

    for season_item in season_items:
        season_episodes = get_season_episodes(season_item)
        episodes_response = build_episodes_response(season_episodes)
        season_response = build_season_response(
            season_item,
            episodes_response,
            season_episodes,
        )

        response[f"season/{season_item.season_number}"] = season_response

        season_response["title"] = response["title"]
        response["related"]["seasons"].append(season_response)
        season_max_progress = season_response.get("max_progress")
        if season_max_progress is None:
            season_max_progress = season_episodes.count()
        num_episodes += season_max_progress

    return num_episodes


def build_season_response(season_item, episodes_response, season_episodes):
    """Build the season response dictionary."""
    fallback_details = {}
    fallback_episode_count = season_episodes.count()
    if fallback_episode_count:
        fallback_details["episodes"] = fallback_episode_count

    details = custom_metadata.build_manual_detail_payload(
        season_item,
        fallback_details=fallback_details,
    )
    return {
        "source": Sources.MANUAL.value,
        "media_id": season_item.media_id,
        "media_type": MediaTypes.SEASON.value,
        "title": season_item.title,
        "season_title": _season_title(season_item),
        "image": season_item.image,
        "season_number": season_item.season_number,
        "episodes": episodes_response,
        "max_progress": custom_metadata.manual_max_progress(
            season_item,
            details,
            fallback_max_progress=fallback_episode_count,
        ),
        "score": None,
        "score_count": None,
        "synopsis": custom_metadata.get_manual_synopsis(season_item),
        "genres": list(season_item.genres or []),
        "details": details,
    }


def get_season_episodes(season):
    """Get all episodes for a season."""
    return models.Item.objects.filter(
        media_id=season.media_id,
        source=Sources.MANUAL.value,
        media_type=MediaTypes.EPISODE.value,
        season_number=season.season_number,
    ).order_by("episode_number")


def episode(media_id, season_number, episode_number):
    """Return the metadata for a manual episode."""
    season_metadata = season(media_id, season_number)
    episode_item = models.Item.objects.filter(
        media_id=media_id,
        source=Sources.MANUAL.value,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_number,
        episode_number=episode_number,
    ).first()

    for season_episode in season_metadata["episodes"]:
        if season_episode["episode_number"] == int(episode_number):
            details = (
                custom_metadata.build_manual_detail_payload(
                    episode_item,
                    fallback_details={"air_date": season_episode.get("air_date")},
                )
                if episode_item
                else {}
            )
            return {
                "source": Sources.MANUAL.value,
                "media_id": media_id,
                "media_type": MediaTypes.EPISODE.value,
                "title": season_metadata["title"],
                "season_title": season_metadata["season_title"],
                "episode_title": season_episode["title"],
                "image": season_episode["image"],
                "synopsis": (
                    custom_metadata.get_manual_synopsis(episode_item)
                    if episode_item
                    else "No synopsis available."
                ),
                "details": details,
            }

    return None


def process_episodes(season_metadata, episodes_in_db):
    """Process the episodes for the selected season."""
    tracked_episodes = {}
    for ep in episodes_in_db:
        episode_number = ep.item.episode_number
        if episode_number not in tracked_episodes:
            tracked_episodes[episode_number] = []
        tracked_episodes[episode_number].append(ep)

    episodes_metadata = []

    for episode_payload in season_metadata["episodes"]:
        episode_number = episode_payload["episode_number"]
        episode_data = {
            "source": Sources.MANUAL.value,
            "media_id": episode_payload["media_id"],
            "media_type": MediaTypes.EPISODE.value,
            "season_number": season_metadata["season_number"],
            "episode_number": episode_number,
            "air_date": episode_payload.get("air_date"),
            "image": episode_payload["image"],
            "title": episode_payload["title"],
            "overview": (
                episode_payload.get("overview")
                or episode_payload.get("synopsis")
                or "No synopsis available."
            ),
            "history": tracked_episodes.get(episode_number, []),
        }
        if episode_payload.get("runtime"):
            episode_data["runtime"] = episode_payload["runtime"]
        episodes_metadata.append(episode_data)

    return episodes_metadata


def build_episodes_response(season_episodes):
    """Build the episodes response list."""
    episodes = []
    for episode_item in season_episodes:
        details = custom_metadata.build_manual_detail_payload(episode_item)
        episodes.append(
            {
                "media_id": episode_item.media_id,
                "source": Sources.MANUAL.value,
                "title": _episode_title(episode_item),
                "image": episode_item.image,
                "episode_number": episode_item.episode_number,
                "air_date": details.get("air_date"),
                "runtime": details.get("runtime"),
                "synopsis": custom_metadata.get_manual_synopsis(episode_item),
                "overview": custom_metadata.get_manual_synopsis(episode_item),
            },
        )
    return episodes
