from django.conf import settings
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from app import credits, helpers, statistics_cache
from app.models import (
    Book,
    Comic,
    CreditRoleType,
    Episode,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Sources,
    Studio,
)
from app.providers import comicvine, hardcover, igdb, mangaupdates, openlibrary, tmdb

logger = __import__("logging").getLogger(__name__)


@require_GET
def person_detail(request, source, person_id, name):
    """Render a provider-backed person or author profile page."""
    del name  # URL slug is cosmetic; person_id is canonical.
    source_dispatch = {
        Sources.TMDB.value: {
            "fetcher": tmdb.person,
            "entries_key": "filmography",
            "tracked_media_types": (
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
            ),
            "source_url": lambda person_id_value: f"https://www.themoviedb.org/person/{person_id_value}",
            "is_author": False,
        },
        Sources.HARDCOVER.value: {
            "fetcher": hardcover.author_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.BOOK.value,),
            "source_url": lambda person_id_value: f"https://hardcover.app/authors/{person_id_value}",
            "is_author": True,
        },
        Sources.OPENLIBRARY.value: {
            "fetcher": openlibrary.author_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.BOOK.value,),
            "source_url": lambda person_id_value: f"https://openlibrary.org/authors/{person_id_value}",
            "is_author": True,
        },
        Sources.COMICVINE.value: {
            "fetcher": comicvine.person_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.COMIC.value,),
            "source_url": lambda person_id_value: f"https://comicvine.gamespot.com/person/4040-{person_id_value}/",
            "is_author": True,
        },
        Sources.MANGAUPDATES.value: {
            "fetcher": mangaupdates.author_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.MANGA.value,),
            "source_url": lambda person_id_value: f"https://www.mangaupdates.com/authors.html?id={person_id_value}",
            "is_author": True,
        },
    }
    source_config = source_dispatch.get(source)
    if not source_config:
        return HttpResponseBadRequest("Person pages are not available for this source.")

    person_metadata = source_config["fetcher"](person_id) or {}
    person = credits.upsert_person_profile(source, person_id, person_metadata)

    person_id_str = str(person_id)
    is_author = source_config["is_author"]
    person_data = {
        "source": source,
        "person_id": person_id_str,
        "name": person_metadata.get("name")
        or (person.name if person else "Unknown Person"),
        "image": person_metadata.get("image")
        or (person.image if person else settings.IMG_NONE),
        "biography": person_metadata.get("biography")
        or (person.biography if person else ""),
        "known_for_department": person_metadata.get("known_for_department")
        or (person.known_for_department if person else ("Author" if is_author else "")),
        "birth_date": person_metadata.get("birth_date")
        or (person.birth_date.isoformat() if person and person.birth_date else None),
        "death_date": person_metadata.get("death_date")
        or (person.death_date.isoformat() if person and person.death_date else None),
        "place_of_birth": person_metadata.get("place_of_birth")
        or (person.place_of_birth if person else ""),
    }

    media_types_for_source = source_config["tracked_media_types"]
    raw_entries = person_metadata.get(source_config["entries_key"], [])
    filmography = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            continue
        media_id_value = raw_entry.get("media_id")
        if media_id_value is None:
            continue
        media_type = raw_entry.get("media_type")
        if media_type is None and len(media_types_for_source) == 1:
            media_type = media_types_for_source[0]
        if media_type not in media_types_for_source:
            continue
        filmography.append(
            {
                **raw_entry,
                "media_id": str(media_id_value),
                "media_type": media_type,
                "source": raw_entry.get("source") or source,
                "title": raw_entry.get("title") or "Unknown Title",
                "image": raw_entry.get("image") or settings.IMG_NONE,
                "year": raw_entry.get("year"),
                "role": raw_entry.get("role") or "",
                "department": raw_entry.get("department") or "",
                "credit_type": raw_entry.get("credit_type") or ("author" if is_author else ""),
                "sort_order": raw_entry.get("sort_order", index),
            },
        )

    if is_author and not filmography:
        fallback_items = Item.objects.filter(
            source=source,
            media_type__in=media_types_for_source,
            person_credits__role_type=CreditRoleType.AUTHOR.value,
            person_credits__person__source=source,
            person_credits__person__source_person_id=person_id_str,
        ).order_by("title").distinct()
        for index, item in enumerate(fallback_items):
            filmography.append(
                {
                    "media_id": str(item.media_id),
                    "source": source,
                    "media_type": item.media_type,
                    "title": item.title,
                    "image": item.image or settings.IMG_NONE,
                    "year": None,
                    "role": "Author",
                    "department": "",
                    "credit_type": "author",
                    "sort_order": index,
                },
            )

    seen_media = set()
    deduped_filmography = []
    for entry in filmography:
        media_key = (entry.get("media_type"), str(entry.get("media_id")))
        if media_key in seen_media:
            continue
        seen_media.add(media_key)
        deduped_filmography.append(entry)
    filmography = deduped_filmography

    tracked_item_map = {}
    if filmography:
        tracked_filters = Q()
        for media_type in media_types_for_source:
            media_ids_for_type = {
                entry["media_id"]
                for entry in filmography
                if entry.get("media_type") == media_type
            }
            if media_ids_for_type:
                tracked_filters |= Q(
                    media_type=media_type,
                    media_id__in=media_ids_for_type,
                )
        if tracked_filters:
            tracked_items = Item.objects.filter(source=source).filter(tracked_filters)
            tracked_item_map = {
                (item.media_type, str(item.media_id)): item
                for item in tracked_items
            }

    credited_tracked_items_by_key = {}
    if request.user.is_authenticated and is_author:
        for model, media_type in (
            (Book, MediaTypes.BOOK.value),
            (Comic, MediaTypes.COMIC.value),
            (Manga, MediaTypes.MANGA.value),
        ):
            tracked_reads = (
                model.objects.filter(
                    user=request.user,
                    item__media_type=media_type,
                    item__person_credits__role_type=CreditRoleType.AUTHOR.value,
                    item__person_credits__person__source=source,
                    item__person_credits__person__source_person_id=person_id_str,
                )
                .filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
                .select_related("item")
                .distinct()
            )
            for tracked_read in tracked_reads:
                item = tracked_read.item
                media_key = (item.media_type, str(item.media_id))
                if media_key in credited_tracked_items_by_key:
                    continue
                credited_tracked_items_by_key[media_key] = item

    if credited_tracked_items_by_key:
        tracked_item_map.update(credited_tracked_items_by_key)

    watched_media_keys = set()
    watched_person_minutes_by_media_key = {}
    person_talent_totals = None
    if request.user.is_authenticated and not is_author:
        person_talent_totals = statistics_cache.get_person_talent_totals(
            request.user,
            source,
            person_id_str,
        )
        watched_person_minutes_by_media_key = (
            person_talent_totals.get("minutes_by_media_key", {})
            if person_talent_totals
            else {}
        )

    if credited_tracked_items_by_key:
        watched_media_keys.update(credited_tracked_items_by_key.keys())

    if request.user.is_authenticated and filmography:
        watched_movie_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.MOVIE.value
        }
        watched_tv_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.TV.value
        }
        watched_book_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.BOOK.value
        }
        watched_comic_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.COMIC.value
        }
        watched_manga_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.MANGA.value
        }

        if watched_movie_media_ids:
            watched_movies = Movie.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.MOVIE.value,
                item__media_id__in=watched_movie_media_ids,
            ).exclude(start_date__isnull=True, end_date__isnull=True)
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_movies.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

        if watched_tv_media_ids:
            watched_tv = Episode.objects.filter(
                related_season__user=request.user,
                end_date__isnull=False,
                related_season__related_tv__item__source=source,
                related_season__related_tv__item__media_type=MediaTypes.TV.value,
                related_season__related_tv__item__media_id__in=watched_tv_media_ids,
            )
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_tv.values_list(
                    "related_season__related_tv__item__media_type",
                    "related_season__related_tv__item__media_id",
                ).distinct()
            )

        if watched_book_media_ids:
            watched_books = Book.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.BOOK.value,
                item__media_id__in=watched_book_media_ids,
            ).filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_books.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

        if watched_comic_media_ids:
            watched_comics = Comic.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.COMIC.value,
                item__media_id__in=watched_comic_media_ids,
            ).filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_comics.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

        if watched_manga_media_ids:
            watched_manga = Manga.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.MANGA.value,
                item__media_id__in=watched_manga_media_ids,
            ).filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_manga.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

    for entry in filmography:
        media_key = (entry.get("media_type"), str(entry.get("media_id")))
        entry["tracked_item"] = tracked_item_map.get(media_key)
        entry["is_watched"] = media_key in watched_media_keys

    watched_filmography = []
    if watched_media_keys:
        seen_watched_media = set()
        for entry in filmography:
            media_key = (entry.get("media_type"), str(entry.get("media_id")))
            if media_key in watched_media_keys and media_key not in seen_watched_media:
                watched_entry = dict(entry)
                watched_minutes = watched_person_minutes_by_media_key.get(media_key, 0)
                if watched_minutes > 0:
                    watched_entry["watched_person_runtime_display"] = (
                        helpers.minutes_to_hhmm(watched_minutes)
                    )
                watched_filmography.append(watched_entry)
                seen_watched_media.add(media_key)

        if is_author and credited_tracked_items_by_key:
            for media_key, tracked_item in credited_tracked_items_by_key.items():
                if media_key in seen_watched_media:
                    continue
                watched_filmography.append(
                    {
                        "media_id": str(tracked_item.media_id),
                        "source": tracked_item.source,
                        "media_type": tracked_item.media_type,
                        "title": tracked_item.title,
                        "image": tracked_item.image or settings.IMG_NONE,
                        "year": (
                            tracked_item.release_datetime.year
                            if tracked_item.release_datetime
                            else None
                        ),
                        "role": "Author",
                        "department": "",
                        "credit_type": "author",
                        "sort_order": len(watched_filmography),
                        "tracked_item": tracked_item,
                        "is_watched": True,
                    },
                )
                seen_watched_media.add(media_key)

    watched_movie_count = sum(
        1 for media_type, _ in watched_media_keys if media_type == MediaTypes.MOVIE.value
    )
    watched_show_count = sum(
        1 for media_type, _ in watched_media_keys if media_type == MediaTypes.TV.value
    )
    watched_book_count = sum(
        1
        for media_type, _ in watched_media_keys
        if media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        )
    )

    from django.urls import reverse

    history_filter_url = (
        f"{reverse('history')}?person_source={source}&person_id={person_id}"
    )
    source_url = source_config["source_url"](person_id_str)

    tracked_plays_count = None
    tracked_hours_count = None
    if request.user.is_authenticated:
        if is_author:
            tracked_plays_count = len(credited_tracked_items_by_key)
        else:
            tracked_plays_count = 0
            if person_talent_totals:
                tracked_plays_count = person_talent_totals.get("plays", 0)
                tracked_hours_count = person_talent_totals.get("watched_time")

    context = {
        "user": request.user,
        "person": person_data,
        "is_author": is_author,
        "watched_filmography": watched_filmography,
        "watched_movie_count": watched_movie_count,
        "watched_show_count": watched_show_count,
        "watched_book_count": watched_book_count,
        "filmography": filmography,
        "history_filter_url": history_filter_url,
        "tracked_plays_count": tracked_plays_count,
        "tracked_hours_count": tracked_hours_count,
        "source": source,
        "source_url": source_url,
    }
    return render(request, "app/person_detail.html", context)


def studio_detail(request, source, studio_id, name):
    """Render a provider-backed studio/company profile page."""
    del name  # URL slug is cosmetic; studio_id is canonical.

    studio = get_object_or_404(
        Studio,
        source=source,
        source_studio_id=str(studio_id),
    )

    studio_profile = (
        igdb.company_profile(studio_id)
        if source == Sources.IGDB.value
        else None
    )

    local_titles = []
    studio_credits = studio.item_credits.select_related("item").order_by(
        "sort_order",
        "item__title",
    )
    for index, studio_credit in enumerate(studio_credits):
        item = studio_credit.item
        if not item:
            continue
        local_titles.append(
            {
                "media_id": str(item.media_id),
                "source": item.source,
                "media_type": item.media_type,
                "title": item.title,
                "image": item.image or settings.IMG_NONE,
                "year": item.release_datetime.year if item.release_datetime else None,
                "role": "",
                "department": "",
                "credit_type": item.media_type,
                "sort_order": (
                    studio_credit.sort_order
                    if studio_credit.sort_order is not None
                    else index
                ),
                "tracked_item": item,
            },
        )

    credited_titles = []
    if studio_profile:
        credited_titles = [
            dict(entry)
            for entry in studio_profile.get("games") or []
            if isinstance(entry, dict)
        ]

    if credited_titles:
        existing_keys = {
            (entry.get("media_type"), str(entry.get("media_id")))
            for entry in credited_titles
        }
        for entry in local_titles:
            media_key = (entry.get("media_type"), str(entry.get("media_id")))
            if media_key not in existing_keys:
                credited_titles.append(entry)
    else:
        credited_titles = local_titles

    if credited_titles:
        game_ids = {
            str(entry.get("media_id"))
            for entry in credited_titles
            if entry.get("media_id") is not None
        }
        tracked_items = Item.objects.filter(
            source=source,
            media_type=MediaTypes.GAME.value,
            media_id__in=game_ids,
        )
        tracked_item_map = {
            (item.media_type, str(item.media_id)): item for item in tracked_items
        }
        for entry in credited_titles:
            media_key = (entry.get("media_type"), str(entry.get("media_id")))
            entry["tracked_item"] = tracked_item_map.get(media_key)

        credited_titles.sort(
            key=lambda row: (
                row.get("year") is None,
                -(row.get("year") or 0),
                row.get("title", "").lower(),
            ),
        )
        for index, entry in enumerate(credited_titles):
            entry["sort_order"] = index

    studio_description = "Studio profile generated from local credits."
    studio_source_url = ""
    studio_founded = None
    studio_developed_count = None
    studio_published_count = None
    if studio_profile:
        studio_description = studio_profile.get("description") or studio_description
        studio_source_url = studio_profile.get("source_url") or ""
        studio_details = studio_profile.get("details") or {}
        studio_founded = studio_details.get("founded")
        studio_developed_count = studio_details.get("developed_count")
        studio_published_count = studio_details.get("published_count")

    context = {
        "user": request.user,
        "studio": studio,
        "source": source,
        "credited_titles": credited_titles,
        "studio_description": studio_description,
        "studio_source_url": studio_source_url,
        "studio_founded": studio_founded,
        "studio_developed_count": studio_developed_count,
        "studio_published_count": studio_published_count,
        "studio_games_count": len(credited_titles),
        "IMG_NONE": settings.IMG_NONE,
    }
    return render(request, "app/studio_detail.html", context)
