import calendar
import logging
import time
from collections import defaultdict
from datetime import date
from urllib.parse import urlencode

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import EmptyPage, Paginator
from django.db.models.functions import ExtractDay, ExtractMonth
from django.db.utils import OperationalError
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_http_methods

from app import cache_utils, helpers, history_cache, history_processor, statistics_cache
from app import statistics as stats
from app.models import BasicMedia, MediaTypes

logger = logging.getLogger(__name__)

_MONTH_CACHE_UNSUPPORTED_FILTER_KEYS = frozenset(
    {
        "artist",
        "person_id",
        "person_source",
        "season",
        "season_number",
        "tv",
    },
)


@require_GET
def history_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the history page for a media item."""
    instance_id = request.GET.get("instance_id")
    if instance_id:
        try:
            media = BasicMedia.objects.get_media(
                request.user,
                media_type,
                instance_id,
            )
            user_medias = [media]
        except (ObjectDoesNotExist, ValueError, TypeError):
            user_medias = BasicMedia.objects.filter_media(
                request.user,
                media_id,
                media_type,
                source,
                season_number=season_number,
                episode_number=episode_number,
            )
    else:
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
            episode_number=episode_number,
        )

    try:
        total_medias = user_medias.count()
    except TypeError:
        total_medias = len(user_medias)
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        history = (
            media.history.filter(end_date__isnull=False)
            if hasattr(media.history, "filter")
            else [h for h in media.history.all() if h.end_date]
        )
        if history:
            media_entry_number = total_medias - index + 1
            timeline_entries.extend(
                history_processor.process_history_entries(
                    history,
                    media_type,
                    media_entry_number,
                    request.user,
                ),
            )
    return render(
        request,
        "app/components/fill_history.html",
        {
            "user": request.user,
            "media_type": media_type,
            "timeline": timeline_entries,
            "total_medias": total_medias,
            "return_url": request.GET.get("return_url", ""),
        },
    )


@require_http_methods(["DELETE"])
def delete_history_record(request, media_type, history_id):
    """Delete a specific history record."""
    try:
        historical_model = apps.get_model(
            app_label="app",
            model_name=f"historical{media_type.lower()}",
        )

        try:
            history_record = historical_model.objects.get(
                history_id=history_id,
                history_user=request.user,
            )
        except historical_model.DoesNotExist:
            history_record = historical_model.objects.get(
                history_id=history_id,
                history_user__isnull=True,
            )
            try:
                BasicMedia.objects.get_media(
                    request.user,
                    media_type.lower(),
                    history_record.id,
                )
            except ObjectDoesNotExist:
                raise historical_model.DoesNotExist(
                    f"History record {history_id} not found for user {request.user}",
                )

        media_instance_id = history_record.id
        start_date = getattr(history_record, "start_date", None)
        end_date = getattr(history_record, "end_date", None)
        created_at = getattr(history_record, "created_at", None)
        media_type_lower = media_type.lower()

        instance_delete_types = {
            MediaTypes.MOVIE.value,
            MediaTypes.EPISODE.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        }
        delete_instance = media_type_lower in instance_delete_types

        logger.info(
            "Attempting to delete history record %s (media_type=%s, media_instance_id=%s, user=%s)",
            str(history_id),
            media_type_lower,
            media_instance_id,
            str(request.user),
        )

        music_id = request.GET.get("music_id")
        podcast_id = request.GET.get("podcast_id")

        if delete_instance:
            try:
                media_instance = BasicMedia.objects.get_media(
                    request.user,
                    media_type_lower,
                    media_instance_id,
                )
            except (ObjectDoesNotExist, ValueError, TypeError):
                logger.exception(
                    "Media instance %s not found for history record %s (media_type=%s, user=%s)",
                    str(media_instance_id),
                    str(history_id),
                    media_type_lower,
                    str(request.user),
                )
                return HttpResponse("Record not found", status=404)

            related_season = (
                getattr(media_instance, "related_season", None)
                if media_type_lower == MediaTypes.EPISODE.value
                else None
            )

            try:
                media_instance.delete()
            except Exception as e:
                logger.error(
                    "Failed to delete media instance %s for history record %s: %s",
                    str(media_instance_id),
                    str(history_id),
                    str(e),
                    exc_info=True,
                )
                return HttpResponse("Failed to delete record", status=500)

            if related_season:
                related_season._sync_status_after_episode_change()
                cache_utils.clear_time_left_cache_for_user(related_season.user_id)
                cache_utils.clear_media_list_cache_for_user(related_season.user_id)

            try:
                model = apps.get_model(app_label="app", model_name=media_type_lower)
                verification_query = model.objects.filter(id=media_instance_id)
                if media_type_lower == MediaTypes.EPISODE.value:
                    verification_query = verification_query.filter(
                        related_season__user=request.user,
                    )
                else:
                    verification_query = verification_query.filter(user=request.user)

                if verification_query.exists():
                    logger.error(
                        "Deletion verification failed: media instance %s still exists after delete() call",
                        str(media_instance_id),
                    )
                    return HttpResponse("Deletion failed", status=500)
            except Exception as e:
                logger.warning(
                    "Could not verify deletion of media instance %s: %s",
                    str(media_instance_id),
                    str(e),
                )
        else:
            try:
                history_record.delete()
            except Exception as e:
                logger.error(
                    "Failed to delete history record %s: %s",
                    str(history_id),
                    str(e),
                    exc_info=True,
                )
                return HttpResponse("Failed to delete record", status=500)

            try:
                verification_query = historical_model.objects.filter(history_id=history_id)
                if verification_query.exists():
                    logger.error(
                        "Deletion verification failed: history record %s still exists after delete() call",
                        str(history_id),
                    )
                    return HttpResponse("Deletion failed", status=500)
            except Exception as e:
                logger.warning(
                    "Could not verify deletion of history record %s: %s",
                    str(history_id),
                    str(e),
                )

        logger.info(
            "Successfully deleted %s %s (media_type=%s, media_instance_id=%s)",
            "media instance" if delete_instance else "history record",
            str(history_id),
            media_type_lower,
            media_instance_id,
        )

        logging_styles = ("sessions", "repeats")
        if media_type_lower in ("game", "boardgame"):
            start_dt = start_date or end_date
            end_dt = end_date or start_date
            history_day_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
        else:
            activity_dt = end_date or start_date or created_at
            history_day_key = history_cache.history_day_key(activity_dt)
            history_day_keys = [history_day_key] if history_day_key else []

        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=logging_styles,
            reason="history_delete",
        )
        statistics_cache.invalidate_statistics_days(
            request.user.id,
            day_values=history_day_keys,
            reason="history_delete",
        )
        statistics_cache.schedule_all_ranges_refresh(request.user.id)

        if music_id and media_type.lower() == "music":
            from app.models import Music
            from users.templatetags.user_tags import user_date_format

            try:
                music = Music.objects.get(id=music_id, user=request.user)
                remaining_history = list(
                    music.history.filter(
                        history_user=request.user,
                    ).order_by("-end_date"),
                ) or list(
                    music.history.filter(
                        history_user__isnull=True,
                    ).order_by("-end_date"),
                )

                remaining_count = len(remaining_history)

                if remaining_count > 0:
                    last_entry = remaining_history[0]
                    last_date_formatted = (
                        user_date_format(last_entry.end_date, request.user)
                        if last_entry.end_date
                        else "No date provided"
                    )

                    if remaining_count == 1:
                        history_text = f"Last listened: {last_date_formatted}"
                    else:
                        history_text = (
                            f"Last listened: {last_date_formatted} "
                            f"• Listened {remaining_count} times"
                        )

                    response = HttpResponse()
                    response.write(
                        f'<p id="track-history-{music_id}" hx-swap-oob="true" '
                        'class="text-xs text-gray-400 mt-2 px-4">'
                        f"{history_text}</p>",
                    )
                    modal_text = (
                        "Listened once"
                        if remaining_count == 1
                        else f"Listened {remaining_count} times"
                    )
                    response.write(
                        f'<p id="modal-listen-count-{music_id}" hx-swap-oob="true" '
                        'class="text-sm text-gray-400 mt-1">'
                        f"{modal_text}</p>",
                    )
                    return response
                response = HttpResponse()
                response.write(
                    f'<p id="track-history-{music_id}" hx-swap-oob="true" '
                    'class="text-xs text-gray-400 mt-2 px-4" style="display: none;"></p>',
                )
                response.write(
                    f'<p id="modal-listen-count-{music_id}" hx-swap-oob="true" '
                    'class="text-sm text-gray-400 mt-1">Not listened yet</p>',
                )
                return response
            except Music.DoesNotExist:
                pass

        if podcast_id and media_type.lower() == "podcast":
            from app.models import Podcast
            from users.templatetags.user_tags import user_date_format

            try:
                podcast = Podcast.objects.get(id=podcast_id, user=request.user)
                remaining_history = list(
                    podcast.history.filter(
                        history_user=request.user,
                    ).order_by("-end_date"),
                ) or list(
                    podcast.history.filter(
                        history_user__isnull=True,
                    ).order_by("-end_date"),
                )

                remaining_count = len(remaining_history)

                if remaining_count > 0:
                    last_entry = remaining_history[0]
                    last_date_formatted = (
                        user_date_format(last_entry.end_date, request.user)
                        if last_entry.end_date
                        else "No date provided"
                    )

                    if remaining_count == 1:
                        history_text = f"Last played: {last_date_formatted}"
                    else:
                        history_text = (
                            f"Last played: {last_date_formatted} "
                            f"• Played {remaining_count} times"
                        )

                    response = HttpResponse()
                    modal_text = (
                        "Played once"
                        if remaining_count == 1
                        else f"Played {remaining_count} times"
                    )
                    response.write(
                        f'<p id="modal-listen-count-{podcast_id}" hx-swap-oob="true" '
                        'class="text-sm text-gray-400 mt-1">'
                        f"{modal_text}</p>",
                    )
                    response["HX-Trigger"] = "history-refresh-start"
                    return response
                response = HttpResponse()
                response.write(
                    f'<p id="modal-listen-count-{podcast_id}" hx-swap-oob="true" '
                    'class="text-sm text-gray-400 mt-1">Not played yet</p>',
                )
                response["HX-Trigger"] = "history-refresh-start"
                return response
            except Podcast.DoesNotExist:
                pass

        response = HttpResponse()
        response["HX-Trigger"] = "history-refresh-start"
        return response

    except historical_model.DoesNotExist:
        logger.exception(
            "History record %s not found for user %s",
            str(history_id),
            str(request.user),
        )
        return HttpResponse("Record not found", status=404)


def _build_anniversary_history_days(user, month, day, logging_style=None):
    day_keys = history_cache.build_history_index(user, logging_style_override=logging_style)
    history_days = []
    for day_key in day_keys:
        try:
            day_date = date.fromisoformat(day_key)
        except ValueError:
            continue
        if day_date.month != month or day_date.day != day:
            continue
        day_payload = history_cache.build_history_day(
            user,
            day_date,
            logging_style_override=logging_style,
        )
        if day_payload and day_payload.get("entries"):
            history_days.append(day_payload)
    return history_days


def _build_release_history_days(user, month=None, day=None, date_filters=None, filters=None):
    active_types = list(getattr(user, "get_active_media_types", list)())
    if not active_types:
        active_types = list(MediaTypes.values)
    include_podcasts = MediaTypes.PODCAST.value in active_types
    active_types = [
        media_type
        for media_type in active_types
        if media_type not in (MediaTypes.EPISODE.value, MediaTypes.PODCAST.value)
    ]

    media_type_filter = (filters or {}).get("media_type")
    include_episodes = True
    if media_type_filter:
        if media_type_filter in (MediaTypes.TV.value, MediaTypes.SEASON.value, MediaTypes.EPISODE.value):
            active_types = []
            include_podcasts = False
        elif media_type_filter == MediaTypes.PODCAST.value:
            active_types = []
            include_podcasts = True
            include_episodes = False
        else:
            active_types = [mt for mt in active_types if mt == media_type_filter]
            include_podcasts = False
            include_episodes = False

    def _matches_genre_filters(item, *, album=None):
        active_filters = filters or {}
        genre_filter = active_filters.get("genre")
        if genre_filter:
            requested = {g.strip().lower() for g in genre_filter.split(",") if g.strip()}
            values = getattr(item, "genres", None) or []
            if album is not None:
                values = list(values) + list(getattr(album, "genres", None) or [])
            if not ({str(g).lower() for g in values} & requested):
                return False
        implied_genre_filter = active_filters.get("implied_genre")
        if implied_genre_filter:
            requested = {
                g.strip().lower()
                for g in implied_genre_filter.split(",")
                if g.strip()
            }
            values = getattr(item, "implied_genres", None) or []
            if album is not None:
                values = list(values) + list(getattr(album, "implied_genres", None) or [])
            if not ({str(g).lower() for g in values} & requested):
                return False
        return True

    start_date = None
    end_date = None
    if date_filters:
        start_date = parse_date(date_filters.get("start_date") or "")
        end_date = parse_date(date_filters.get("end_date") or "")

    release_days = defaultdict(list)
    seen_item_ids = set()
    for media_type in active_types:
        model = apps.get_model("app", media_type)
        queryset = (
            model.objects.filter(user=user, item__release_datetime__isnull=False)
            .select_related("item")
        )
        if month and day:
            queryset = queryset.annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                queryset = queryset.filter(item__release_datetime__date__gte=start_date)
            if end_date:
                queryset = queryset.filter(item__release_datetime__date__lte=end_date)

        for media in queryset:
            item = getattr(media, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            if not _matches_genre_filters(item):
                continue
            seen_item_ids.add(item.id)
            release_dt = getattr(item, "release_datetime", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            entry = {
                "item": item,
                "media_type": item.media_type,
                "title": item.title,
                "display_title": item.title,
                "poster": item.image,
                "played_at_local": localized,
                "entry_key": f"release-{item.id}-{release_date.isoformat()}",
            }
            release_days[release_date].append(entry)

    if include_episodes:
        Episode = apps.get_model("app", "Episode")
        episode_qs = (
            Episode.objects.filter(
                related_season__user=user,
                item__release_datetime__isnull=False,
            )
            .select_related(
                "item",
                "related_season__item",
                "related_season__related_tv__item",
            )
        )
        if month and day:
            episode_qs = episode_qs.annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                episode_qs = episode_qs.filter(item__release_datetime__date__gte=start_date)
            if end_date:
                episode_qs = episode_qs.filter(item__release_datetime__date__lte=end_date)

        for episode in episode_qs:
            episode_item = getattr(episode, "item", None)
            if not episode_item or episode_item.id in seen_item_ids:
                continue
            if not _matches_genre_filters(
                episode_item,
                album=None,
            ):
                continue
            seen_item_ids.add(episode_item.id)
            release_dt = getattr(episode_item, "release_datetime", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            season_item = getattr(episode.related_season, "item", None)
            tv_item = getattr(getattr(episode.related_season, "related_tv", None), "item", None)
            title = (
                episode_item.title
                or (season_item.title if season_item else None)
                or (tv_item.title if tv_item else "")
            )
            display_title = history_cache._get_episode_display_title(episode)
            entry = {
                "item": episode_item,
                "media_type": MediaTypes.EPISODE.value,
                "title": title,
                "display_title": display_title or title,
                "poster": history_cache._get_episode_poster(episode),
                "played_at_local": localized,
                "entry_key": f"release-episode-{episode.id}-{release_date.isoformat()}",
            }
            release_days[release_date].append(entry)

    if include_podcasts:
        Podcast = apps.get_model("app", "Podcast")
        podcast_base = Podcast.objects.filter(user=user).select_related("item", "episode", "show")
        podcast_qs = podcast_base.filter(episode__published__isnull=False)
        if month and day:
            podcast_qs = podcast_qs.annotate(
                release_month=ExtractMonth("episode__published"),
                release_day=ExtractDay("episode__published"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                podcast_qs = podcast_qs.filter(episode__published__date__gte=start_date)
            if end_date:
                podcast_qs = podcast_qs.filter(episode__published__date__lte=end_date)

        for podcast in podcast_qs:
            item = getattr(podcast, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            if not _matches_genre_filters(item):
                continue
            release_dt = getattr(getattr(podcast, "episode", None), "published", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            show = None
            if getattr(podcast, "episode", None) and podcast.episode.show:
                show = podcast.episode.show
            if not show:
                show = podcast.show
            poster = settings.IMG_NONE
            if show and show.image:
                poster = show.image
            elif item.image:
                poster = item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            entry = {
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "display_title": title,
                "show": show,
                "poster": poster,
                "played_at_local": localized,
                "entry_key": f"release-podcast-{podcast.id}-{release_date.isoformat()}",
            }
            seen_item_ids.add(item.id)
            release_days[release_date].append(entry)

        podcast_fallback_qs = podcast_base.filter(
            episode__published__isnull=True,
            item__release_datetime__isnull=False,
        )
        if month and day:
            podcast_fallback_qs = podcast_fallback_qs.annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                podcast_fallback_qs = podcast_fallback_qs.filter(
                    item__release_datetime__date__gte=start_date,
                )
            if end_date:
                podcast_fallback_qs = podcast_fallback_qs.filter(
                    item__release_datetime__date__lte=end_date,
                )

        for podcast in podcast_fallback_qs:
            item = getattr(podcast, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            if not _matches_genre_filters(item):
                continue
            release_dt = getattr(item, "release_datetime", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            show = None
            if getattr(podcast, "episode", None) and podcast.episode.show:
                show = podcast.episode.show
            if not show:
                show = podcast.show
            poster = settings.IMG_NONE
            if show and show.image:
                poster = show.image
            elif item.image:
                poster = item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            entry = {
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "display_title": title,
                "show": show,
                "poster": poster,
                "played_at_local": localized,
                "entry_key": f"release-podcast-{podcast.id}-{release_date.isoformat()}",
            }
            seen_item_ids.add(item.id)
            release_days[release_date].append(entry)

    history_days = []
    for release_date, entries in sorted(
        release_days.items(),
        key=lambda item: item[0],
        reverse=True,
    ):
        entries.sort(key=lambda entry: entry.get("played_at_local"), reverse=True)
        release_display_dt = entries[0]["played_at_local"]
        history_days.append(
            {
                "date": release_date,
                "weekday": formats.date_format(release_display_dt, "l"),
                "date_display": formats.date_format(release_display_dt, "F j, Y"),
                "entries": entries,
                "total_minutes": 0,
                "total_runtime_display": f"{len(entries)} release{'s' if len(entries) != 1 else ''}",
                "release_count": len(entries),
            },
        )
    return history_days


def _filter_history_by_enabled_media_types(history_days, user):
    """Filter history entries to only include enabled media types."""
    enabled_types = user.get_enabled_media_types()
    if not enabled_types:
        return history_days

    allowed_types = set(enabled_types)
    if MediaTypes.TV.value in allowed_types:
        allowed_types.add(MediaTypes.EPISODE.value)
        allowed_types.add(MediaTypes.SEASON.value)

    filtered_days = []
    for day in history_days:
        if isinstance(day, dict):
            entries = day.get("entries", [])
            filtered_entries = [
                entry for entry in entries if entry.get("media_type") in allowed_types
            ]
            if filtered_entries:
                filtered_day = day.copy()
                filtered_day["entries"] = filtered_entries
                filtered_days.append(filtered_day)
        else:
            filtered_days.append(day)

    return filtered_days


def _can_use_cached_month_history(
    history_mode,
    filters,
    date_filters,
    anniversary_month,
    anniversary_day,
):
    if history_mode != "activity":
        return False
    if date_filters or anniversary_month or anniversary_day:
        return False
    if any(key in filters for key in _MONTH_CACHE_UNSUPPORTED_FILTER_KEYS):
        return False
    if filters.get("media_id") or filters.get("source"):
        return False

    return True


def _cached_history_entry_matches_filters(entry, filters):
    entry = entry or {}
    item = entry.get("item") or {}
    album = entry.get("album") or {}
    show = entry.get("show") or {}
    entry_media_type = entry.get("media_type")
    media_type_filter = filters.get("media_type")
    if media_type_filter:
        if media_type_filter == MediaTypes.TV.value:
            if entry_media_type not in {
                MediaTypes.EPISODE.value,
                MediaTypes.SEASON.value,
            }:
                return False
        elif entry_media_type != media_type_filter:
            return False

    genre_filter = filters.get("genre")
    if genre_filter:
        genre_filters = {g.strip().lower() for g in genre_filter.split(",") if g.strip()}
        genres = entry.get("genres") or item.get("genres") or []
        item_genre_set = {str(g).lower() for g in genres}
        if not (item_genre_set & genre_filters):
            return False
    implied_genre_filter = filters.get("implied_genre")
    if implied_genre_filter:
        implied_genre_filters = {
            g.strip().lower()
            for g in implied_genre_filter.split(",")
            if g.strip()
        }
        implied_genres = entry.get("implied_genres") or item.get("implied_genres") or []
        item_implied_genre_set = {str(g).lower() for g in implied_genres}
        if not (item_implied_genre_set & implied_genre_filters):
            return False

    album_filter = filters.get("album")
    if album_filter is not None:
        if entry_media_type != MediaTypes.MUSIC.value:
            return False
        if album.get("id") != album_filter:
            return False

    podcast_show_filter = filters.get("podcast_show")
    if podcast_show_filter is not None:
        if entry_media_type != MediaTypes.PODCAST.value:
            return False
        if show.get("id") != podcast_show_filter:
            return False

    target_media_id = filters.get("media_id")
    if target_media_id is not None and str(item.get("media_id")) != str(target_media_id):
        return False

    target_source = filters.get("source")
    if target_source is not None and str(item.get("source")) != str(target_source):
        return False

    return True


def _filter_cached_history_days(history_days, filters):
    if not filters:
        return history_days

    filtered_days = []
    for day in history_days:
        if not isinstance(day, dict):
            continue

        filtered_entries = [
            entry
            for entry in day.get("entries", [])
            if _cached_history_entry_matches_filters(entry, filters)
        ]
        if not filtered_entries:
            continue

        total_minutes = sum(entry.get("runtime_minutes") or 0 for entry in filtered_entries)
        filtered_day = day.copy()
        filtered_day["entries"] = filtered_entries
        filtered_day["total_minutes"] = total_minutes
        filtered_day["total_runtime_display"] = (
            helpers.minutes_to_hhmm(total_minutes)
            if total_minutes
            else "0min"
        )
        filtered_days.append(filtered_day)

    return filtered_days


@require_GET
def history_genres(request):
    """Return sorted list of unique genres from the user's tracked items."""
    from django.http import JsonResponse

    from app.models import Book, BoardGame, Comic, Episode, Game, Manga, Movie, Music, Podcast

    def _is_valid_genre(value) -> bool:
        s = str(value).strip()
        return bool(s) and not s.lstrip("-").isdigit()

    genres: set[str] = set()
    implied_genres: set[str] = set()
    # Book, Comic, and Manga use Library of Congress subject headings in their genres
    # field rather than real genre names, so exclude them from the genre list.
    for ModelClass in [Movie, Game, Music, BoardGame, Podcast]:
        for genres_list in ModelClass.objects.filter(user=request.user).values_list(
            "item__genres", flat=True
        ):
            if genres_list:
                genres.update(str(g).strip() for g in genres_list if _is_valid_genre(g))
        for implied_genres_list in ModelClass.objects.filter(user=request.user).values_list(
            "item__implied_genres", flat=True,
        ):
            if implied_genres_list:
                implied_genres.update(
                    str(g).strip() for g in implied_genres_list if _is_valid_genre(g)
                )

    for genres_list in Episode.objects.filter(
        related_season__user=request.user
    ).values_list("related_season__related_tv__item__genres", flat=True):
        if genres_list:
            genres.update(str(g).strip() for g in genres_list if _is_valid_genre(g))

    genres.discard("")
    implied_genres.discard("")
    return JsonResponse(
        {
            "genres": sorted(genres, key=str.lower),
            "implied_genres": sorted(implied_genres, key=str.lower),
        },
    )


@require_GET
def history(request):
    """Show a day-by-day history of episode and movie plays."""
    try:
        view_start = time.perf_counter()
        history_mode = request.GET.get("history_mode")
        if history_mode != "release":
            history_mode = "activity"

        filters = {}
        int_params = ["album", "artist", "tv", "season", "season_number", "podcast_show"]
        str_params = [
            "genre",
            "implied_genre",
            "media_type",
            "media_id",
            "source",
            "person_source",
            "person_id",
        ]
        for param in int_params:
            value = request.GET.get(param)
            if value:
                try:
                    filters[param] = int(value)
                except (TypeError, ValueError):
                    pass
        for param in str_params:
            value = request.GET.get(param)
            if value:
                filters[param] = value

        logging_style = request.GET.get("logging_style")
        if logging_style not in ("sessions", "repeats"):
            logging_style = None

        date_filters = {}
        start_date_str = request.GET.get("start-date")
        end_date_str = request.GET.get("end-date")
        if start_date_str:
            date_filters["start_date"] = start_date_str
        if end_date_str:
            date_filters["end_date"] = end_date_str

        anniversary_month = request.GET.get("month")
        anniversary_day = request.GET.get("day")
        try:
            anniversary_month = int(anniversary_month) if anniversary_month else None
            anniversary_day = int(anniversary_day) if anniversary_day else None
        except (TypeError, ValueError):
            anniversary_month = None
            anniversary_day = None

        now = timezone.localtime()
        try:
            view_year = int(request.GET.get("year", now.year))
            view_month = int(request.GET.get("m", now.month))
            if view_month < 1 or view_month > 12:
                view_month = now.month
        except (TypeError, ValueError):
            view_year = now.year
            view_month = now.month

        logger.info(
            "history_view_start user_id=%s year=%s month=%s filters=%s date_filters=%s logging_style=%s",
            request.user.id,
            view_year,
            view_month,
            filters,
            date_filters,
            logging_style,
        )

        use_month_cache = _can_use_cached_month_history(
            history_mode,
            filters,
            date_filters,
            anniversary_month,
            anniversary_day,
        )
        history_refreshing = False

        if use_month_cache:
            history_days, cache_meta = history_cache.get_month_history(
                request.user,
                view_year,
                view_month,
                logging_style_override=logging_style,
            )
            history_days = _filter_cached_history_days(history_days, filters)
            history_refreshing = cache_meta.get("refreshing", False)
            history_days = _filter_history_by_enabled_media_types(history_days, request.user)

            page_obj = None
            current_page = 1
            total_pages = 1
            total_days = len(history_days)

            if view_month == 1:
                prev_year, prev_month = view_year - 1, 12
            else:
                prev_year, prev_month = view_year, view_month - 1
            if view_month == 12:
                next_year, next_month = view_year + 1, 1
            else:
                next_year, next_month = view_year, view_month + 1

            prev_month_name = calendar.month_abbr[prev_month]
            next_month_name = calendar.month_abbr[next_month]
            is_current_month = view_year == now.year and view_month == now.month
            show_next_month = (
                next_year < now.year
                or (next_year == now.year and next_month <= now.month)
            )
        else:
            try:
                page_number = int(request.GET.get("page", 1))
            except (TypeError, ValueError):
                page_number = 1

            if history_mode == "release":
                history_days_all = _build_release_history_days(
                    request.user,
                    month=anniversary_month,
                    day=anniversary_day,
                    date_filters=date_filters,
                    filters=filters,
                )
                history_refreshing = False
            elif anniversary_month and anniversary_day:
                history_days_all = _build_anniversary_history_days(
                    request.user,
                    month=anniversary_month,
                    day=anniversary_day,
                    logging_style=logging_style,
                )
                history_refreshing = False
            else:
                history_days_all = history_cache.get_history_days(
                    request.user,
                    filters=filters,
                    date_filters=date_filters,
                    logging_style_override=logging_style,
                )

            history_days_all = _filter_history_by_enabled_media_types(
                history_days_all,
                request.user,
            )

            paginator = Paginator(history_days_all, history_cache.HISTORY_DAYS_PER_PAGE)

            if paginator.count == 0:
                page_obj = None
                history_days = []
                current_page = 1
                total_pages = 1
                total_days = 0
            else:
                try:
                    page_obj = paginator.page(page_number)
                except EmptyPage:
                    page_obj = paginator.page(paginator.num_pages)

                history_days = page_obj.object_list
                current_page = page_obj.number
                total_pages = paginator.num_pages
                total_days = paginator.count

            prev_year = prev_month = next_year = next_month = None
            prev_month_name = next_month_name = None
            show_next_month = False
            is_current_month = False

        active_filters = filters.copy()
        if date_filters.get("start_date"):
            active_filters["start-date"] = date_filters["start_date"]
        if date_filters.get("end_date"):
            active_filters["end-date"] = date_filters["end_date"]
        if logging_style:
            active_filters["logging_style"] = logging_style
        if anniversary_month and anniversary_day:
            active_filters["month"] = anniversary_month
            active_filters["day"] = anniversary_day
        if history_mode == "release":
            active_filters["history_mode"] = "release"
        month_nav_query = urlencode(active_filters)
        month_name = calendar.month_name[view_month] if use_month_cache else None

        context = {
            "user": request.user,
            "history_days": history_days,
            "page_obj": page_obj,
            "current_page": current_page,
            "total_pages": total_pages,
            "total_days": total_days,
            "active_filters": active_filters,
            "history_refreshing": history_refreshing,
            "history_mode": history_mode,
            "use_month_view": use_month_cache,
            "view_year": view_year,
            "view_month": view_month,
            "month_name": month_name,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "prev_month_name": prev_month_name,
            "next_year": next_year,
            "next_month": next_month,
            "next_month_name": next_month_name,
            "show_next_month": show_next_month,
            "is_current_month": is_current_month,
            "current_year": now.year,
            "current_month_num": now.month,
            "month_nav_query": month_nav_query,
        }
        day_entry_counts = []
        total_entries = 0
        for day in history_days:
            entries = day.get("entries", []) if isinstance(day, dict) else getattr(day, "entries", [])
            count = len(entries)
            total_entries += count
            day_entry_counts.append((day.get("date_display") or day.get("date"), count))
        top_days = sorted(day_entry_counts, key=lambda item: item[1], reverse=True)[:3]
        logger.info(
            "history_page_entry_counts user_id=%s page=%s total_entries=%s top_days=%s",
            request.user.id,
            current_page,
            total_entries,
            top_days,
        )
        render_start = time.perf_counter()
        logger.info(
            "history_render_start user_id=%s page=%s",
            request.user.id,
            current_page,
        )
        response = render(request, "app/history.html", context)
        render_ms = (time.perf_counter() - render_start) * 1000
        response_bytes = len(response.content)
        logger.info(
            "history_render_end user_id=%s page=%s render_ms=%.2f response_bytes=%s",
            request.user.id,
            current_page,
            render_ms,
            response_bytes,
        )
        logger.info(
            "history_view_end user_id=%s page=%s total_days=%s page_days=%s total_pages=%s elapsed_ms=%.2f response_bytes=%s",
            request.user.id,
            current_page,
            total_days,
            len(history_days),
            total_pages,
            (time.perf_counter() - view_start) * 1000,
            response_bytes,
        )
        return response
    except OperationalError as error:
        logger.error("Database error in history view: %s", error, exc_info=True)
        context = {
            "user": request.user,
            "history_days": [],
            "page_obj": None,
            "current_page": 1,
            "total_pages": 0,
            "total_days": 0,
            "days_per_page": history_cache.HISTORY_DAYS_PER_PAGE,
            "active_filters": {},
            "database_error": True,
            "history_refreshing": False,
        }
        return render(request, "app/history.html", context)
