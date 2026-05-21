import logging
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db.models import Q
from django.db.models.functions import TruncDate
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from app import history_cache
from app.models import Album, BasicMedia, Episode, Season

logger = logging.getLogger(__name__)


def _collect_music_history_day_keys_for_album_ids(user, album_ids):
    """Return distinct history day keys for plays tied to the given album ids."""
    normalized_album_ids = sorted({album_id for album_id in album_ids or [] if album_id})
    if not normalized_album_ids:
        return []

    HistoricalMusic = apps.get_model("app", "HistoricalMusic")
    history_days = (
        HistoricalMusic.objects.filter(
            Q(history_user=user) | Q(history_user__isnull=True),
            album_id__in=normalized_album_ids,
            end_date__isnull=False,
        )
        .annotate(day=TruncDate("end_date"))
        .values_list("day", flat=True)
        .distinct()
    )
    return sorted(
        {
            history_cache.history_day_key(day_value)
            for day_value in history_days
            if day_value
        },
    )


def _collect_music_history_day_keys_for_artist(user, artist):
    """Return distinct history day keys for plays tied to an artist's albums."""
    album_ids = Album.objects.filter(artist=artist).values_list("id", flat=True)
    return _collect_music_history_day_keys_for_album_ids(user, album_ids)


@require_POST
def update_media_score(request, media_type, instance_id):
    """Update the user's score for a media item."""
    media = BasicMedia.objects.get_media(
        request.user,
        media_type,
        instance_id,
    )

    score_raw = request.POST.get("score")
    toggle = request.POST.get("toggle")
    score = None
    if score_raw is not None:
        score_raw = score_raw.strip()
        if score_raw and score_raw.lower() != "null":
            try:
                score = Decimal(score_raw)
            except (InvalidOperation, TypeError):
                return HttpResponseBadRequest("Invalid score.")
            score = request.user.scale_score_for_storage(score)
            if score is None:
                return HttpResponseBadRequest("Invalid score.")

    if toggle and score is not None and media.score == score:
        score = None

    media.score = score
    media.save()
    logger.info(
        "%s score updated to %s",
        media,
        score,
    )

    return render(
        request,
        "app/components/detail_score_chip_slot.html",
        {
            "media": media.item,
            "current_instance": media,
            "media_type": media_type,
            "user": request.user,
            "user_medias": [media],
            "public_view": False,
            "csrf_token": request.META.get("CSRF_COOKIE", ""),
            "score_chip_slot_oob": True,
        },
    )


@login_required
@require_POST
def update_episode_score(request, season_id, episode_number):
    """Update the user's score for a specific episode."""
    season = get_object_or_404(Season, id=season_id, user=request.user)

    score_raw = request.POST.get("score")
    toggle = request.POST.get("toggle")
    score = None
    if score_raw is not None:
        score_raw = score_raw.strip()
        if score_raw and score_raw.lower() != "null":
            try:
                score = Decimal(score_raw)
            except (InvalidOperation, TypeError):
                return HttpResponseBadRequest("Invalid score.")
            score = request.user.scale_score_for_storage(score)
            if score is None:
                return HttpResponseBadRequest("Invalid score.")

    episodes = Episode.objects.filter(
        related_season=season,
        item__episode_number=episode_number,
    )

    if toggle and score is not None:
        existing = episodes.values_list("score", flat=True).first()
        if existing == score:
            score = None

    episodes.update(score=score)
    logger.info(
        "Episode S%sE%s score updated to %s for user %s",
        season.item.season_number,
        episode_number,
        score,
        request.user,
    )

    return JsonResponse(
        {
            "success": True,
            "score": request.user.format_score_for_display(score) if score is not None else None,
        },
    )


@require_POST
def update_artist_score(request, artist_id):
    """Update the user's score for an artist."""
    from app.models import Artist, ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)

    tracker, _ = ArtistTracker.objects.get_or_create(
        user=request.user,
        artist=artist,
    )

    score_raw = request.POST.get("score")
    if score_raw is None:
        return HttpResponseBadRequest("Invalid score.")
    try:
        score = Decimal(score_raw)
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Invalid score.")
    score = request.user.scale_score_for_storage(score)
    if score is None:
        return HttpResponseBadRequest("Invalid score.")
    tracker.score = score
    tracker.save()
    logger.info(
        "%s score updated to %s",
        artist,
        score,
    )

    history_day_keys = _collect_music_history_day_keys_for_artist(request.user, artist)
    if history_day_keys:
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=("sessions", "repeats"),
            reason="artist_score_change",
        )

    return JsonResponse(
        {
            "success": True,
            "score": request.user.format_score_for_display(score),
        },
    )


@require_POST
def update_album_score(request, album_id):
    """Update the user's score for an album."""
    from app.models import Album, AlbumTracker

    album = get_object_or_404(Album, id=album_id)

    tracker, _ = AlbumTracker.objects.get_or_create(
        user=request.user,
        album=album,
    )

    score_raw = request.POST.get("score")
    if score_raw is None:
        return HttpResponseBadRequest("Invalid score.")
    try:
        score = Decimal(score_raw)
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Invalid score.")
    score = request.user.scale_score_for_storage(score)
    if score is None:
        return HttpResponseBadRequest("Invalid score.")
    tracker.score = score
    tracker.save()
    logger.info(
        "%s score updated to %s",
        album,
        score,
    )

    history_day_keys = _collect_music_history_day_keys_for_album_ids(
        request.user,
        [album.id],
    )
    if history_day_keys:
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=("sessions", "repeats"),
            reason="album_score_change",
        )

    return JsonResponse(
        {
            "success": True,
            "score": request.user.format_score_for_display(score),
        },
    )
