"""Talent and credit aggregation for statistics: top cast, crew, studios, and per-person totals."""

import logging
from collections import Counter, defaultdict
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from app import credits as credit_helpers
from app import statistics as stats
from app.models import (
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    Movie,
    Person,
    PersonGender,
    Sources,
)

logger = logging.getLogger(__name__)

# Controls the maximum items shown in every top-N statistics card.
# Change this ONE value to resize all top cards simultaneously.
STATISTICS_TOP_N = 50
# Cross-type global top-rated heap — intentionally same value but a distinct card.
STATISTICS_TOP_RATED_OVERALL = 50


def _safe_runtime_minutes(value):
    if not value:
        return 0
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 0
    # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
    if minutes >= 999998:
        return 0
    return minutes


def _tv_episode_play_rows(user, start_date, end_date):
    """Return watched TV episode rows and the season/show items they touch."""
    episodes_qs = Episode.objects.filter(
        related_season__user=user,
        end_date__isnull=False,
    )
    if start_date:
        episodes_qs = episodes_qs.filter(end_date__gte=start_date)
    if end_date:
        episodes_qs = episodes_qs.filter(end_date__lte=end_date)

    episode_play_rows = []
    season_item_ids = set()
    tv_item_ids = set()
    for episode_item_id, season_item_id, tv_item_id, runtime_minutes in episodes_qs.values_list(
        "item_id",
        "related_season__item_id",
        "related_season__related_tv__item_id",
        "item__runtime_minutes",
    ).iterator():
        if not tv_item_id:
            continue
        episode_play_rows.append(
            (
                episode_item_id,
                season_item_id,
                tv_item_id,
                _safe_runtime_minutes(runtime_minutes),
            ),
        )
        if season_item_id:
            season_item_ids.add(season_item_id)
        if tv_item_id:
            tv_item_ids.add(tv_item_id)

    season_items_with_cast_credits = set()
    season_items_with_director_credits = set()
    season_items_with_writer_credits = set()
    season_items_with_usable_credits = credit_helpers.usable_credits_backfill_item_ids(
        season_item_ids,
    )
    if season_item_ids:
        for credit in ItemPersonCredit.objects.filter(item_id__in=season_item_ids).iterator():
            if credit.role_type == CreditRoleType.CAST.value:
                season_items_with_cast_credits.add(credit.item_id)
                continue
            if credit.role_type == CreditRoleType.CREW.value:
                if _is_director_credit(credit):
                    season_items_with_director_credits.add(credit.item_id)
                if _is_writer_credit(credit):
                    season_items_with_writer_credits.add(credit.item_id)

    return SimpleNamespace(
        episode_play_rows=episode_play_rows,
        season_item_ids=season_item_ids,
        tv_item_ids=tv_item_ids,
        season_items_with_cast_credits=season_items_with_cast_credits,
        season_items_with_director_credits=season_items_with_director_credits,
        season_items_with_writer_credits=season_items_with_writer_credits,
        season_items_with_usable_credits=season_items_with_usable_credits,
    )


def _resolve_missing_credit_item_ids(item_ids):
    """Return TMDB movie/show/season/episode item IDs that still need credits backfill."""
    return credit_helpers.missing_credits_backfill_item_ids(item_ids)


def _is_director_credit(credit) -> bool:
    department = (credit.department or "").strip().lower()
    role = (credit.role or "").strip().lower()
    if department == "directing":
        return True
    return "director" in role


def _is_writer_credit(credit) -> bool:
    department = (credit.department or "").strip().lower()
    role = (credit.role or "").strip().lower()
    if department == "writing":
        return True
    return any(keyword in role for keyword in ("writer", "screenplay", "story", "teleplay", "script"))


def get_person_talent_totals(user, person_source, person_id, start_date=None, end_date=None):
    """Return stats-style totals for a single person's primary talent bucket."""
    if not user or not person_source or person_id is None:
        return None

    person = Person.objects.filter(
        source=person_source,
        source_person_id=str(person_id),
    ).first()
    if not person:
        return None

    movie_play_counts = Counter()
    movie_watch_minutes = Counter()
    tv_episode_rows = _tv_episode_play_rows(
        user,
        start_date,
        end_date,
    )
    episode_play_rows = tv_episode_rows.episode_play_rows
    season_item_ids = tv_episode_rows.season_item_ids
    tv_item_ids = tv_episode_rows.tv_item_ids
    season_items_with_cast_credits = tv_episode_rows.season_items_with_cast_credits
    season_items_with_director_credits = tv_episode_rows.season_items_with_director_credits
    season_items_with_writer_credits = tv_episode_rows.season_items_with_writer_credits
    season_items_with_usable_credits = tv_episode_rows.season_items_with_usable_credits

    movies_qs = Movie.objects.filter(
        user=user,
    ).filter(
        Q(end_date__isnull=False) | Q(start_date__isnull=False),
    )
    if start_date:
        movies_qs = movies_qs.filter(
            Q(end_date__gte=start_date)
            | (Q(end_date__isnull=True) & Q(start_date__gte=start_date)),
        )
    if end_date:
        movies_qs = movies_qs.filter(
            Q(end_date__lte=end_date)
            | (Q(end_date__isnull=True) & Q(start_date__lte=end_date)),
        )
    for item_id, runtime_minutes in movies_qs.values_list("item_id", "item__runtime_minutes").iterator():
        if item_id:
            movie_play_counts[item_id] += 1
            movie_watch_minutes[item_id] += _safe_runtime_minutes(runtime_minutes)

    if not movie_play_counts and not episode_play_rows:
        return None

    movie_item_ids = set(movie_play_counts.keys())
    show_item_ids = set(tv_item_ids)
    episode_item_ids = {episode_item_id for episode_item_id, _, _, _ in episode_play_rows if episode_item_id}
    played_item_ids = movie_item_ids | show_item_ids | season_item_ids | episode_item_ids
    if not played_item_ids:
        return None
    item_rows = list(
        Item.objects.filter(
            id__in=played_item_ids,
        ).values_list("id", "media_type", "media_id", "source"),
    )
    item_media_type_by_id = {
        item_id: media_type
        for item_id, media_type, _media_id, _source in item_rows
    }
    item_media_key_by_id = {
        item_id: (media_type, str(media_id))
        for item_id, media_type, media_id, _source in item_rows
    }
    item_source_by_id = {
        item_id: source
        for item_id, _media_type, _media_id, source in item_rows
    }

    tv_items_with_usable_credits = credit_helpers.usable_credits_backfill_item_ids(tv_item_ids)

    missing_credit_item_ids = credit_helpers.missing_credits_backfill_item_ids(played_item_ids)
    if missing_credit_item_ids:
        try:
            from app.tasks import enqueue_credits_backfill_items

            enqueue_credits_backfill_items(missing_credit_item_ids, countdown=3)
        except Exception as exc:  # pragma: no cover - best effort scheduling
            logger.debug(
                "person_talent_credits_backfill_schedule_failed user_id=%s person_source=%s person_id=%s error=%s",
                user.id,
                person_source,
                person_id,
                exc,
            )

    actor_credit_item_ids = set()
    actress_credit_item_ids = set()
    director_credit_item_ids = set()
    writer_credit_item_ids = set()

    person_credits = ItemPersonCredit.objects.filter(
        item_id__in=played_item_ids,
        person_id=person.id,
    )
    for credit in person_credits:
        item_media_type = item_media_type_by_id.get(credit.item_id)
        if not item_media_type:
            continue
        if credit.role_type == CreditRoleType.CAST.value:
            if item_media_type == MediaTypes.TV.value and not credit_helpers.is_usable_tv_show_credit(
                item_source_by_id.get(credit.item_id),
                credit.role_type,
                credit.sort_order,
            ):
                continue
            if person.gender == PersonGender.MALE.value:
                actor_credit_item_ids.add(credit.item_id)
            elif person.gender == PersonGender.FEMALE.value:
                actress_credit_item_ids.add(credit.item_id)
            continue

        if credit.role_type == CreditRoleType.CREW.value:
            if _is_director_credit(credit):
                director_credit_item_ids.add(credit.item_id)
            if _is_writer_credit(credit):
                writer_credit_item_ids.add(credit.item_id)

    bucket_plays = Counter()
    bucket_minutes = Counter()
    bucket_movie_items = defaultdict(set)
    bucket_show_items = defaultdict(set)
    bucket_minutes_by_media_key = defaultdict(lambda: defaultdict(int))

    role_sources = (
        ("actor", actor_credit_item_ids, season_items_with_cast_credits, CreditRoleType.CAST.value),
        ("actress", actress_credit_item_ids, season_items_with_cast_credits, CreditRoleType.CAST.value),
        ("director", director_credit_item_ids, season_items_with_director_credits, CreditRoleType.CREW.value),
        ("writer", writer_credit_item_ids, season_items_with_writer_credits, CreditRoleType.CREW.value),
    )

    for item_id, plays in movie_play_counts.items():
        if plays <= 0:
            continue
        watched_minutes = int(movie_watch_minutes.get(item_id, 0))
        media_key = item_media_key_by_id.get(item_id)
        for bucket, item_ids, _season_credit_item_ids, _role_type in role_sources:
            if item_id not in item_ids:
                continue
            bucket_plays[bucket] += plays
            bucket_minutes[bucket] += watched_minutes
            bucket_movie_items[bucket].add(item_id)
            if media_key:
                bucket_minutes_by_media_key[bucket][media_key] += watched_minutes

    for episode_item_id, season_item_id, tv_item_id, watched_minutes in episode_play_rows:
        if not tv_item_id:
            continue
        media_key = item_media_key_by_id.get(tv_item_id)
        show_has_usable_credits = tv_item_id in tv_items_with_usable_credits
        for bucket, item_ids, season_credit_item_ids, role_type in role_sources:
            season_has_usable_credits = (
                season_item_id in season_credit_item_ids
                and season_item_id in season_items_with_usable_credits
            )
            is_match = (
                episode_item_id in item_ids
                or season_item_id in item_ids
                or (
                    tv_item_id in item_ids
                    and credit_helpers.should_count_tv_show_credit_for_episode(
                        item_source_by_id.get(tv_item_id),
                        role_type,
                        None,
                        season_has_usable_credits,
                        show_has_usable_credits,
                    )
                )
            )
            if not is_match:
                continue
            bucket_plays[bucket] += 1
            bucket_minutes[bucket] += watched_minutes
            bucket_show_items[bucket].add(tv_item_id)
            if media_key:
                bucket_minutes_by_media_key[bucket][media_key] += watched_minutes

    bucket_payloads = {}
    for bucket, _item_ids, _season_credit_item_ids, _role_type in role_sources:
        unique_movies = len(bucket_movie_items.get(bucket, set()))
        unique_shows = len(bucket_show_items.get(bucket, set()))
        watched_minutes = int(bucket_minutes.get(bucket, 0))
        bucket_payloads[bucket] = {
            "bucket": bucket,
            "plays": int(bucket_plays.get(bucket, 0)),
            "watched_minutes": watched_minutes,
            "watched_time": stats._format_hours_minutes(watched_minutes, user.duration_format),
            "unique_movies": unique_movies,
            "unique_shows": unique_shows,
            "unique_titles": unique_movies + unique_shows,
            "minutes_by_media_key": dict(bucket_minutes_by_media_key.get(bucket, {})),
        }

    nonzero_buckets = [
        bucket
        for bucket, payload in bucket_payloads.items()
        if payload["plays"] > 0 or payload["watched_minutes"] > 0
    ]
    if not nonzero_buckets:
        return None

    known_for_department = (person.known_for_department or "").strip().lower()
    preferred_order = []
    if known_for_department == "acting":
        if person.gender == PersonGender.MALE.value:
            preferred_order = ["actor"]
        elif person.gender == PersonGender.FEMALE.value:
            preferred_order = ["actress"]
        else:
            preferred_order = ["actor", "actress"]
    elif known_for_department == "directing":
        preferred_order = ["director"]
    elif known_for_department == "writing":
        preferred_order = ["writer"]

    selected_bucket = None
    for bucket in preferred_order:
        if bucket in nonzero_buckets:
            selected_bucket = bucket
            break

    if selected_bucket is None:
        selected_bucket = max(
            nonzero_buckets,
            key=lambda bucket: (
                bucket_payloads[bucket]["plays"],
                bucket_payloads[bucket]["watched_minutes"],
                bucket_payloads[bucket]["unique_titles"],
            ),
        )

    return bucket_payloads.get(selected_bucket)


def _aggregate_top_talent(user, start_date, end_date, limit=STATISTICS_TOP_N, schedule_missing_backfill=True):
    """Aggregate top cast/crew/studio rollups from watched movie and TV plays."""
    movie_play_counts = Counter()
    movie_watch_minutes = Counter()
    valid_sort_modes = ("plays", "time", "titles")
    sort_by = getattr(user, "top_talent_sort_by", "plays")
    if sort_by not in valid_sort_modes:
        sort_by = "plays"

    def _empty_talent_bucket():
        return {
            "top_actors": [],
            "top_actresses": [],
            "top_directors": [],
            "top_writers": [],
            "top_studios": [],
        }

    tv_episode_rows = _tv_episode_play_rows(
        user,
        start_date,
        end_date,
    )
    episode_play_rows = tv_episode_rows.episode_play_rows
    season_item_ids = tv_episode_rows.season_item_ids
    tv_item_ids = tv_episode_rows.tv_item_ids
    season_items_with_cast_credits = tv_episode_rows.season_items_with_cast_credits
    season_items_with_director_credits = tv_episode_rows.season_items_with_director_credits
    season_items_with_writer_credits = tv_episode_rows.season_items_with_writer_credits
    season_items_with_usable_credits = tv_episode_rows.season_items_with_usable_credits

    # Movie plays: count completed/dated movie entries.
    movies_qs = Movie.objects.filter(
        user=user,
    ).filter(
        Q(end_date__isnull=False) | Q(start_date__isnull=False),
    )
    if start_date:
        movies_qs = movies_qs.filter(
            Q(end_date__gte=start_date)
            | (Q(end_date__isnull=True) & Q(start_date__gte=start_date)),
        )
    if end_date:
        movies_qs = movies_qs.filter(
            Q(end_date__lte=end_date)
            | (Q(end_date__isnull=True) & Q(start_date__lte=end_date)),
        )
    for item_id, runtime_minutes in movies_qs.values_list("item_id", "item__runtime_minutes").iterator():
        if item_id:
            movie_play_counts[item_id] += 1
            movie_watch_minutes[item_id] += _safe_runtime_minutes(runtime_minutes)

    if not movie_play_counts and not episode_play_rows:
        by_sort = {mode: _empty_talent_bucket() for mode in valid_sort_modes}
        selected_payload = by_sort.get(sort_by, _empty_talent_bucket())
        return {
            "sort_by": sort_by,
            "by_sort": by_sort,
            **selected_payload,
        }

    movie_item_ids = set(movie_play_counts.keys())
    show_item_ids = set(tv_item_ids)
    episode_item_ids = {episode_item_id for episode_item_id, _, _, _ in episode_play_rows if episode_item_id}
    played_item_ids = movie_item_ids | show_item_ids | season_item_ids | episode_item_ids
    item_rows = list(
        Item.objects.filter(
            id__in=played_item_ids,
        ).values_list("id", "media_type", "media_id", "source"),
    )
    item_media_type_by_id = {
        item_id: media_type
        for item_id, media_type, _media_id, _source in item_rows
    }
    item_source_by_id = {
        item_id: source
        for item_id, _media_type, _media_id, source in item_rows
    }

    cast_actor_ids_by_item = defaultdict(set)
    cast_actress_ids_by_item = defaultdict(set)
    director_ids_by_item = defaultdict(set)
    writer_ids_by_item = defaultdict(set)
    studio_ids_by_item = defaultdict(set)
    people_by_id = {}
    studios_by_id = {}

    person_credits = ItemPersonCredit.objects.filter(item_id__in=played_item_ids).select_related("person")
    for credit in person_credits:
        person = credit.person
        if not person:
            continue
        people_by_id[person.id] = person

        if credit.role_type == CreditRoleType.CAST.value:
            item_media_type = item_media_type_by_id.get(credit.item_id)
            if item_media_type == MediaTypes.TV.value and not credit_helpers.is_usable_tv_show_credit(
                item_source_by_id.get(credit.item_id),
                credit.role_type,
                credit.sort_order,
            ):
                continue
            if person.gender == PersonGender.MALE.value:
                cast_actor_ids_by_item[credit.item_id].add(person.id)
            elif person.gender == PersonGender.FEMALE.value:
                cast_actress_ids_by_item[credit.item_id].add(person.id)
            continue

        if credit.role_type == CreditRoleType.CREW.value:
            if _is_director_credit(credit):
                director_ids_by_item[credit.item_id].add(person.id)
            if _is_writer_credit(credit):
                writer_ids_by_item[credit.item_id].add(person.id)

    studio_item_ids = movie_item_ids | show_item_ids
    studio_credits = ItemStudioCredit.objects.filter(item_id__in=studio_item_ids).select_related("studio")
    for credit in studio_credits:
        studio = credit.studio
        if not studio:
            continue
        studios_by_id[studio.id] = studio
        studio_ids_by_item[credit.item_id].add(studio.id)

    tv_items_with_usable_credits = credit_helpers.usable_credits_backfill_item_ids(tv_item_ids)
    missing_credit_item_ids = credit_helpers.missing_credits_backfill_item_ids(played_item_ids)

    if missing_credit_item_ids and schedule_missing_backfill:
        try:
            from app.tasks import enqueue_credits_backfill_items

            enqueue_credits_backfill_items(missing_credit_item_ids, countdown=3)
        except Exception as exc:  # pragma: no cover - best effort scheduling
            logger.debug(
                "top_talent_credits_backfill_schedule_failed user_id=%s items=%s error=%s",
                user.id,
                len(missing_credit_item_ids),
                exc,
            )

    actor_counts = Counter()
    actor_minutes = Counter()
    actress_counts = Counter()
    actress_minutes = Counter()
    director_counts = Counter()
    director_minutes = Counter()
    writer_counts = Counter()
    writer_minutes = Counter()
    studio_counts = Counter()
    studio_minutes = Counter()
    actor_movie_items = defaultdict(set)
    actor_show_items = defaultdict(set)
    actress_movie_items = defaultdict(set)
    actress_show_items = defaultdict(set)
    director_movie_items = defaultdict(set)
    director_show_items = defaultdict(set)
    writer_movie_items = defaultdict(set)
    writer_show_items = defaultdict(set)
    studio_movie_items = defaultdict(set)
    studio_show_items = defaultdict(set)

    for item_id, plays in movie_play_counts.items():
        if plays <= 0:
            continue
        watched_minutes = int(movie_watch_minutes.get(item_id, 0))
        for person_id in cast_actor_ids_by_item.get(item_id, ()):
            actor_counts[person_id] += plays
            actor_minutes[person_id] += watched_minutes
            actor_movie_items[person_id].add(item_id)
        for person_id in cast_actress_ids_by_item.get(item_id, ()):
            actress_counts[person_id] += plays
            actress_minutes[person_id] += watched_minutes
            actress_movie_items[person_id].add(item_id)
        for person_id in director_ids_by_item.get(item_id, ()):
            director_counts[person_id] += plays
            director_minutes[person_id] += watched_minutes
            director_movie_items[person_id].add(item_id)
        for person_id in writer_ids_by_item.get(item_id, ()):
            writer_counts[person_id] += plays
            writer_minutes[person_id] += watched_minutes
            writer_movie_items[person_id].add(item_id)
        for studio_id in studio_ids_by_item.get(item_id, ()):
            studio_counts[studio_id] += plays
            studio_minutes[studio_id] += watched_minutes
            studio_movie_items[studio_id].add(item_id)

    for episode_item_id, season_item_id, tv_item_id, watched_minutes in episode_play_rows:
        if not tv_item_id:
            continue

        season_has_cast_credits = (
            season_item_id in season_items_with_cast_credits
            and season_item_id in season_items_with_usable_credits
        )
        season_has_director_credits = (
            season_item_id in season_items_with_director_credits
            and season_item_id in season_items_with_usable_credits
        )
        season_has_writer_credits = (
            season_item_id in season_items_with_writer_credits
            and season_item_id in season_items_with_usable_credits
        )
        show_has_usable_credits = tv_item_id in tv_items_with_usable_credits
        actor_ids = cast_actor_ids_by_item.get(episode_item_id, set()) | cast_actor_ids_by_item.get(
            season_item_id,
            set(),
        )
        if credit_helpers.should_count_tv_show_credit_for_episode(
            item_source_by_id.get(tv_item_id),
            CreditRoleType.CAST.value,
            None,
            season_has_cast_credits,
            show_has_usable_credits,
        ):
            actor_ids |= cast_actor_ids_by_item.get(tv_item_id, set())
        for person_id in actor_ids:
            actor_counts[person_id] += 1
            actor_minutes[person_id] += watched_minutes
            actor_show_items[person_id].add(tv_item_id)

        actress_ids = cast_actress_ids_by_item.get(episode_item_id, set()) | cast_actress_ids_by_item.get(
            season_item_id,
            set(),
        )
        if credit_helpers.should_count_tv_show_credit_for_episode(
            item_source_by_id.get(tv_item_id),
            CreditRoleType.CAST.value,
            None,
            season_has_cast_credits,
            show_has_usable_credits,
        ):
            actress_ids |= cast_actress_ids_by_item.get(tv_item_id, set())
        for person_id in actress_ids:
            actress_counts[person_id] += 1
            actress_minutes[person_id] += watched_minutes
            actress_show_items[person_id].add(tv_item_id)

        director_ids = director_ids_by_item.get(episode_item_id, set()) | director_ids_by_item.get(
            season_item_id,
            set(),
        )
        if credit_helpers.should_count_tv_show_credit_for_episode(
            item_source_by_id.get(tv_item_id),
            CreditRoleType.CREW.value,
            None,
            season_has_director_credits,
            show_has_usable_credits,
        ):
            director_ids |= director_ids_by_item.get(tv_item_id, set())
        for person_id in director_ids:
            director_counts[person_id] += 1
            director_minutes[person_id] += watched_minutes
            director_show_items[person_id].add(tv_item_id)

        writer_ids = writer_ids_by_item.get(episode_item_id, set()) | writer_ids_by_item.get(
            season_item_id,
            set(),
        )
        if credit_helpers.should_count_tv_show_credit_for_episode(
            item_source_by_id.get(tv_item_id),
            CreditRoleType.CREW.value,
            None,
            season_has_writer_credits,
            show_has_usable_credits,
        ):
            writer_ids |= writer_ids_by_item.get(tv_item_id, set())
        for person_id in writer_ids:
            writer_counts[person_id] += 1
            writer_minutes[person_id] += watched_minutes
            writer_show_items[person_id].add(tv_item_id)

        for studio_id in studio_ids_by_item.get(tv_item_id, ()):
            studio_counts[studio_id] += 1
            studio_minutes[studio_id] += watched_minutes
            studio_show_items[studio_id].add(tv_item_id)

    def _person_sort_key(person_id, plays, minutes, movie_items_by_person, show_items_by_person, mode):
        unique_movies = len(movie_items_by_person.get(person_id, set()))
        unique_shows = len(show_items_by_person.get(person_id, set()))
        unique_titles = unique_movies + unique_shows
        person = people_by_id.get(person_id)
        name_key = person.name.lower() if person else ""
        if mode == "time":
            return (-minutes, -plays, -unique_titles, name_key)
        if mode == "titles":
            return (-unique_titles, -plays, -minutes, name_key)
        return (-plays, -minutes, -unique_titles, name_key)

    def _studio_sort_key(studio_id, plays, minutes, movie_items_by_studio, show_items_by_studio, mode):
        unique_movies = len(movie_items_by_studio.get(studio_id, set()))
        unique_shows = len(show_items_by_studio.get(studio_id, set()))
        unique_titles = unique_movies + unique_shows
        studio = studios_by_id.get(studio_id)
        name_key = studio.name.lower() if studio else ""
        if mode == "time":
            return (-minutes, -plays, -unique_titles, name_key)
        if mode == "titles":
            return (-unique_titles, -plays, -minutes, name_key)
        return (-plays, -minutes, -unique_titles, name_key)

    def _sorted_people(counter_obj, minute_counter, movie_items_by_person, show_items_by_person, mode):
        ranked = sorted(
            counter_obj.items(),
            key=lambda row: _person_sort_key(
                row[0],
                row[1],
                int(minute_counter.get(row[0], 0)),
                movie_items_by_person,
                show_items_by_person,
                mode,
            ),
        )[:limit]
        payload = []
        for person_id, plays in ranked:
            person = people_by_id.get(person_id)
            if not person:
                continue
            watched_minutes = int(minute_counter.get(person_id, 0))
            unique_movies = len(movie_items_by_person.get(person_id, set()))
            unique_shows = len(show_items_by_person.get(person_id, set()))
            payload.append(
                {
                    "name": person.name,
                    "image": person.image or settings.IMG_NONE,
                    "source": person.source,
                    "person_id": person.source_person_id,
                    "plays": int(plays),
                    "watched_minutes": watched_minutes,
                    "watched_time": stats._format_hours_minutes(watched_minutes, user.duration_format),
                    "unique_movies": unique_movies,
                    "unique_shows": unique_shows,
                    "unique_titles": unique_movies + unique_shows,
                },
            )
        return payload

    def _sorted_studios(counter_obj, minute_counter, movie_items_by_studio, show_items_by_studio, mode):
        ranked = sorted(
            counter_obj.items(),
            key=lambda row: _studio_sort_key(
                row[0],
                row[1],
                int(minute_counter.get(row[0], 0)),
                movie_items_by_studio,
                show_items_by_studio,
                mode,
            ),
        )[:limit]
        payload = []
        for studio_id, plays in ranked:
            studio = studios_by_id.get(studio_id)
            if not studio:
                continue
            watched_minutes = int(minute_counter.get(studio_id, 0))
            unique_movies = len(movie_items_by_studio.get(studio_id, set()))
            unique_shows = len(show_items_by_studio.get(studio_id, set()))
            payload.append(
                {
                    "name": studio.name,
                    "logo": studio.logo or settings.IMG_NONE,
                    "source": studio.source,
                    "studio_id": studio.source_studio_id,
                    "plays": int(plays),
                    "watched_minutes": watched_minutes,
                    "watched_time": stats._format_hours_minutes(watched_minutes, user.duration_format),
                    "unique_movies": unique_movies,
                    "unique_shows": unique_shows,
                    "unique_titles": unique_movies + unique_shows,
                },
            )
        return payload

    by_sort = {}
    for mode in valid_sort_modes:
        by_sort[mode] = {
            "top_actors": _sorted_people(
                actor_counts,
                actor_minutes,
                actor_movie_items,
                actor_show_items,
                mode,
            ),
            "top_actresses": _sorted_people(
                actress_counts,
                actress_minutes,
                actress_movie_items,
                actress_show_items,
                mode,
            ),
            "top_directors": _sorted_people(
                director_counts,
                director_minutes,
                director_movie_items,
                director_show_items,
                mode,
            ),
            "top_writers": _sorted_people(
                writer_counts,
                writer_minutes,
                writer_movie_items,
                writer_show_items,
                mode,
            ),
            "top_studios": _sorted_studios(
                studio_counts,
                studio_minutes,
                studio_movie_items,
                studio_show_items,
                mode,
            ),
        }

    selected_payload = by_sort.get(sort_by, _empty_talent_bucket())
    return {
        "sort_by": sort_by,
        "by_sort": by_sort,
        **selected_payload,
    }
