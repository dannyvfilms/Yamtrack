"""Local catalog projection for the Stremio addon."""

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote

from django.db.models import F, Max

from app.models import TV, MediaTypes, Movie, Season, Sources, Status
from lists.models import CustomList, CustomListItem

PAGE_SIZE = 100
IMDB_ID_PATTERN = re.compile(r"^tt[0-9]+$")


@dataclass(frozen=True)
class CatalogSpec:
    """Stable Stremio catalog configuration and its local source rule."""

    stremio_type: str
    catalog_id: str
    media_type: str
    preferred_list_name: str = ""
    display_name: str = ""
    statuses: tuple[str, ...] = field(default_factory=tuple)


CATALOG_SPECS = (
    CatalogSpec(
        stremio_type="movie",
        catalog_id="floppy-watchlist-movies",
        media_type=MediaTypes.MOVIE.value,
        preferred_list_name="Movies",
    ),
    CatalogSpec(
        stremio_type="series",
        catalog_id="floppy-watchlist-series",
        media_type=MediaTypes.TV.value,
        preferred_list_name="Series",
    ),
    CatalogSpec(
        stremio_type="movie",
        catalog_id="floppy-history-movies",
        media_type=MediaTypes.MOVIE.value,
        display_name="History",
        statuses=(Status.COMPLETED.value,),
    ),
    CatalogSpec(
        stremio_type="series",
        catalog_id="floppy-history-series",
        media_type=MediaTypes.TV.value,
        display_name="History",
        statuses=(Status.COMPLETED.value,),
    ),
    CatalogSpec(
        stremio_type="movie",
        catalog_id="floppy-in-progress-movies",
        media_type=MediaTypes.MOVIE.value,
        display_name="In Progress",
        statuses=(Status.IN_PROGRESS.value,),
    ),
    CatalogSpec(
        stremio_type="series",
        catalog_id="floppy-in-progress-series",
        media_type=MediaTypes.TV.value,
        display_name="In Progress",
        statuses=(Status.IN_PROGRESS.value,),
    ),
    CatalogSpec(
        stremio_type="series",
        catalog_id="floppy-planning-series",
        media_type=MediaTypes.TV.value,
        display_name="Planning",
        statuses=(Status.PLANNING.value,),
    ),
)

TRACKED_MODELS = {
    MediaTypes.MOVIE.value: Movie,
    MediaTypes.TV.value: TV,
}

CONFIG_SEPARATOR = ","

DEFAULT_CATALOG_IDS = (
    "floppy-watchlist-movies",
    "floppy-watchlist-series",
    "floppy-history-movies",
    "floppy-history-series",
    "floppy-in-progress-movies",
    "floppy-in-progress-series",
)


def resolve_addon_credential(token):
    """Return (user, grant) for an add-on URL token, or (None, None).

    Accepts a catalog grant first, then falls back to the legacy account token
    so existing installs keep working. The fallback is the deprecation path,
    not the design: an account token in a URL grants full API access.
    """
    from integrations.models import CatalogGrant
    from users.models import User

    if not token:
        return (None, None)

    grant = CatalogGrant.objects.select_related("user").filter(token=token).first()
    if grant is not None:
        if not grant.is_valid():
            return (None, None)
        return (grant.user, grant)

    user = User.objects.filter(token=token).first()
    return (user, None) if user is not None else (None, None)


def touch_grant(grant, *, interval_minutes=60):
    """Record grant use, at most once an hour.

    Stremio polls catalogs continuously; writing a row per request would make
    this the busiest table in the install for no added information.
    """
    from django.utils import timezone

    now = timezone.now()
    if grant.last_used_at and (now - grant.last_used_at).total_seconds() < (
        interval_minutes * 60
    ):
        return
    type(grant).objects.filter(pk=grant.pk).update(last_used_at=now)
    grant.last_used_at = now


def manifest_catalogs_for_grant(user, grant, selected=None):
    """Build manifest catalogs limited to what the grant covers.

    Composed with the install URL's own selection rather than replacing it: the
    URL says which catalogs this install wants, the grant says which it is
    allowed, and a catalog needs both.
    """
    catalogs = manifest_catalogs(user, selected)
    if grant is None:
        return catalogs
    return [entry for entry in catalogs if grant.allows_catalog(entry["id"])]


def parse_catalog_config(config):
    """Return the catalog ids selected by an install URL config segment.

    Order is preserved so the install URL also decides the order catalogs
    are published in. Unknown and duplicate ids are ignored so a stale URL
    keeps working, and an empty or fully unrecognised segment falls back
    to the defaults.
    """
    if not config:
        return DEFAULT_CATALOG_IDS

    supported = {spec.catalog_id for spec in CATALOG_SPECS}
    selected = []
    for part in unquote(config).split(CONFIG_SEPARATOR):
        catalog_id = part.strip()
        if catalog_id in supported and catalog_id not in selected:
            selected.append(catalog_id)

    return tuple(selected) if selected else DEFAULT_CATALOG_IDS


def get_catalog_spec(stremio_type, catalog_id):
    """Return the matching supported catalog, if any."""
    return next(
        (
            spec
            for spec in CATALOG_SPECS
            if (spec.stremio_type, spec.catalog_id)
            == (stremio_type, catalog_id)
        ),
        None,
    )


def select_source_list(user, spec):
    """Select the oldest owned preferred list, then the oldest owned Watchlist."""
    owned_lists = CustomList.objects.filter(owner=user)
    source_list = (
        owned_lists.filter(name__iexact=spec.preferred_list_name)
        .order_by("id")
        .first()
    )
    if source_list is not None:
        return source_list

    return owned_lists.filter(name__iexact="Watchlist").order_by("id").first()


def catalog_display_name(user, spec):
    """Return the manifest name for a catalog, by source rule."""
    if spec.statuses:
        return spec.display_name

    source_list = select_source_list(user, spec)
    if source_list is not None:
        return source_list.name
    return spec.preferred_list_name


def catalog_options(user):
    """Return every catalog with its display label, for the configure page."""
    return [
        {
            "id": spec.catalog_id,
            "label": catalog_display_name(user, spec),
            "stremio_type": spec.stremio_type,
        }
        for spec in CATALOG_SPECS
    ]


def manifest_catalogs(user, selected=None):
    """Build manifest catalogs in the order the install URL asked for."""
    enabled = selected if selected is not None else DEFAULT_CATALOG_IDS
    specs = {spec.catalog_id: spec for spec in CATALOG_SPECS}

    catalogs = []
    for catalog_id in enabled:
        spec = specs.get(catalog_id)
        if spec is None:
            continue
        catalogs.append(
            {
                "type": spec.stremio_type,
                "id": spec.catalog_id,
                "name": f"Floppy: {catalog_display_name(user, spec)}",
                "extra": [{"name": "skip", "isRequired": False}],
            }
        )
    return catalogs


def parse_skip(extra):
    """Parse the optional Stremio extra segment and return a non-negative skip."""
    if not extra:
        return 0

    try:
        pairs = parse_qsl(
            unquote(extra),
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as error:
        message = "Malformed catalog extra arguments"
        raise ValueError(message) from error

    if len(pairs) != 1 or pairs[0][0] != "skip":
        message = "Only one skip argument is supported"
        raise ValueError(message)

    value = pairs[0][1]
    if not value.isdecimal():
        message = "skip must be a non-negative integer"
        raise ValueError(message)

    try:
        return int(value)
    except ValueError as error:
        message = "skip must be a non-negative integer"
        raise ValueError(message) from error


def local_imdb_id(item):
    """Resolve an item's IMDb id without network, cache, or database writes."""
    if item.source == Sources.IMDB.value:
        media_id = str(item.media_id)
        if IMDB_ID_PATTERN.fullmatch(media_id):
            return media_id

    imdb_id = str((item.provider_external_ids or {}).get("imdb_id") or "")
    if IMDB_ID_PATTERN.fullmatch(imdb_id):
        return imdb_id

    return None


def catalog_readiness(user):
    """Return per-catalog publishable/unresolved counts for the settings page."""
    readiness = []
    for spec in CATALOG_SPECS:
        if spec.statuses:
            items = status_source_items(user, spec)
        else:
            if select_source_list(user, spec) is None:
                continue
            items = list_source_items(user, spec)
        total = 0
        publishable = 0
        for item in items:
            total += 1
            if local_imdb_id(item) is not None:
                publishable += 1
        if total:
            readiness.append(
                {
                    "noun": "movies" if spec.stremio_type == "movie" else "series",
                    "list_name": catalog_display_name(user, spec),
                    "total": total,
                    "publishable": publishable,
                    "unresolved": total - publishable,
                },
            )
    return readiness


def build_metas(items, spec, skip):
    """Return one page of publishable metas and the scanned unresolved count."""
    metas = []
    publishable_seen = 0
    unresolved_count = 0
    for item in items:
        imdb_id = local_imdb_id(item)
        if imdb_id is None:
            unresolved_count += 1
            continue

        if publishable_seen < skip:
            publishable_seen += 1
            continue

        meta = {"id": imdb_id, "type": spec.stremio_type, "name": item.title}
        if item.image:
            meta["poster"] = item.image
        metas.append(meta)
        if len(metas) == PAGE_SIZE:
            break

    return metas, unresolved_count


def list_source_items(user, spec):
    """Yield items from the catalog's source list, newest membership first."""
    source_list = select_source_list(user, spec)
    if source_list is None:
        return

    memberships = (
        CustomListItem.objects.filter(
            custom_list=source_list,
            item__media_type=spec.media_type,
        )
        .select_related("item")
        .order_by("-date_added", "-id")
    )

    seen_item_ids = set()
    for membership in memberships.iterator():
        seen_item_ids.add(membership.item.pk)
        yield membership.item

    if source_list.is_smart and spec.media_type == MediaTypes.TV.value:
        for parent_item in smart_season_parent_items(user, source_list):
            if parent_item.pk in seen_item_ids:
                continue
            seen_item_ids.add(parent_item.pk)
            yield parent_item


def smart_season_parent_items(user, source_list):
    """Yield parent TV items for smart-list seasons, newest season release first."""
    seen_item_ids = set()
    seasons = (
        Season.objects.filter(
            user=user,
            item__customlistitem__custom_list=source_list,
            item__media_type=MediaTypes.SEASON.value,
        )
        .select_related("item", "related_tv__item")
        .order_by(
            F("item__release_datetime").desc(nulls_last=True),
            "related_tv__item__title",
            "related_tv__item_id",
            "-item__customlistitem__id",
        )
    )
    for season in seasons.iterator():
        parent_item = season.related_tv.item
        parent_item_id = parent_item.pk
        if parent_item_id in seen_item_ids:
            continue
        seen_item_ids.add(parent_item_id)
        yield parent_item


def last_watched_queryset(model, media_type, user, statuses):
    """Filter tracked rows and expose a sortable last-watched date.

    Movie stores end_date directly. TV derives it through properties over
    its seasons and episodes, so the stored episode dates are aggregated
    into an annotation instead.
    """
    tracked = model.objects.filter(user=user, status__in=statuses)
    if media_type == MediaTypes.TV.value:
        return tracked.annotate(last_watched=Max("seasons__episodes__end_date"))
    return tracked.annotate(last_watched=F("end_date"))


def status_source_items(user, spec):
    """Yield tracked items matching the catalog's statuses, latest watched first."""
    model = TRACKED_MODELS.get(spec.media_type)
    if model is None:
        return

    tracked = (
        last_watched_queryset(model, spec.media_type, user, spec.statuses)
        .select_related("item")
        .order_by(F("last_watched").desc(nulls_last=True), "-id")
    )
    for entry in tracked.iterator():
        yield entry.item


def project_catalog(user, spec, skip):
    """Return one page of publishable metas and the scanned unresolved count."""
    if spec.statuses:
        items = status_source_items(user, spec)
    else:
        items = list_source_items(user, spec)

    return build_metas(items, spec, skip)


def project_meta(user, stremio_type, imdb_id):
    """Return the publishable meta for one item the user actually tracks.

    Scoped to the user's own library on purpose. This endpoint is reachable by
    anyone holding the install URL, so answering for arbitrary ids would turn a
    catalog grant into an open metadata proxy over the whole item table.

    Provider fields stay as Floppy holds them; nothing is fetched here, so a
    metadata provider's terms are not extended by publishing this.
    """
    media_types = [
        spec.media_type for spec in CATALOG_SPECS if spec.stremio_type == stremio_type
    ]
    if not media_types:
        return None

    owned_list_ids = CustomList.objects.filter(owner=user).values_list("id", flat=True)
    membership = (
        CustomListItem.objects.filter(
            custom_list_id__in=list(owned_list_ids),
            item__media_type__in=media_types,
        )
        .select_related("item")
        .order_by("-date_added", "-id")
    )

    for entry in membership.iterator():
        item = entry.item
        if local_imdb_id(item) != imdb_id:
            continue

        meta = {
            "id": imdb_id,
            "type": stremio_type,
            "name": item.title,
        }
        if item.image:
            meta["poster"] = item.image
            meta["background"] = item.image
        if getattr(item, "synopsis", None):
            meta["description"] = item.synopsis
        return meta

    return None

