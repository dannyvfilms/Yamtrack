"""Backfill persisted Trakt popularity metadata for tracked titles."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from app.models import (
    TRAKT_POPULARITY_BACKFILL_VERSION,
    MediaTypes,
    MetadataBackfillField,
)
from app.providers import trakt as trakt_provider
from app.services import trakt_popularity
from app.tasks import _record_backfill_failure, _record_backfill_success


class Command(BaseCommand):
    """Populate Trakt popularity metadata for tracked movie, TV, and anime items."""

    help = "Backfill stored Trakt popularity metadata for tracked movie, TV, and anime items"

    def add_arguments(self, parser):
        parser.add_argument(
            "--media-types",
            default=f"{MediaTypes.MOVIE.value},{MediaTypes.TV.value},{MediaTypes.ANIME.value}",
            help="Comma-separated media types to backfill (default: movie,tv,anime)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional max item count",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Refresh all eligible tracked items, not only missing snapshots",
        )
        parser.add_argument(
            "--recompute-scores",
            action="store_true",
            help="Recompute score and rank from stored rating/votes (no Trakt API calls)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show the items that would refresh without writing",
        )

    def handle(self, *_args, **options):
        media_types = self._parse_media_types(options["media_types"])
        limit = options.get("limit")
        force = bool(options.get("force"))
        dry_run = bool(options.get("dry_run"))
        recompute_scores = bool(options.get("recompute_scores"))

        if recompute_scores:
            self._handle_recompute_scores(
                media_types=media_types,
                limit=limit,
                dry_run=dry_run,
            )
            return

        if not trakt_provider.is_configured():
            self.stdout.write(
                self.style.WARNING("TRAKT_API is not configured; skipping Trakt popularity backfill."),
            )
            return

        if force:
            queryset = trakt_popularity.tracked_items_queryset(
                media_types=media_types,
            ).order_by("trakt_popularity_fetched_at", "id")
            items = list(queryset[:limit] if limit else queryset)
        else:
            items = trakt_popularity.select_items_for_refresh(
                limit=limit,
                media_types=media_types,
                missing_only=True,
            )

        if not items:
            self.stdout.write(self.style.SUCCESS("No tracked items need Trakt popularity backfill."))
            return

        updated = 0
        failed = 0
        for index, item in enumerate(items, start=1):
            route_media_type = trakt_popularity.route_media_type_for_item(item)
            if dry_run:
                self.stdout.write(
                    f"[{index}/{len(items)}] would refresh item_id={item.id} route_media_type={route_media_type} title={item.title}",
                )
                continue

            try:
                result = trakt_popularity.refresh_trakt_popularity(
                    item,
                    route_media_type=route_media_type,
                    force=force,
                )
                _record_backfill_success(
                    item,
                    MetadataBackfillField.TRAKT_POPULARITY,
                    strategy_version=TRAKT_POPULARITY_BACKFILL_VERSION,
                )
                updated += 1
                self.stdout.write(
                    (
                        f"[{index}/{len(items)}] updated item_id={item.id} "
                        f"rank=#{result['rank']} votes={result['votes']} "
                        f"match={result.get('matched_id_type') or 'cached'} "
                        f"title={item.title}"
                    ),
                )
            except Exception as error:  # noqa: BLE001
                failed += 1
                _record_backfill_failure(
                    item,
                    MetadataBackfillField.TRAKT_POPULARITY,
                    str(error),
                )
                self.stderr.write(
                    f"[{index}/{len(items)}] failed item_id={item.id} title={item.title}: {error}",
                )

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete: {len(items)} item(s) matched."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Backfill complete: updated {updated} item(s), failed {failed}."),
            )

    def _handle_recompute_scores(self, *, media_types, limit, dry_run):
        from app.models import Item

        queryset = (
            trakt_popularity.tracked_items_queryset(media_types=media_types)
            .exclude(trakt_rating__isnull=True)
            .exclude(trakt_rating_count__isnull=True)
            .order_by("id")
        )
        items = list(queryset[:limit] if limit else queryset)

        if not items:
            self.stdout.write(self.style.SUCCESS("No items with stored Trakt rating data found."))
            return

        updated = 0
        for index, item in enumerate(items, start=1):
            new_score = trakt_popularity.compute_popularity_score(
                item.trakt_rating,
                item.trakt_rating_count,
            )
            new_rank = trakt_popularity.estimate_rank_from_score(new_score)
            if dry_run:
                self.stdout.write(
                    f"[{index}/{len(items)}] item_id={item.id} "
                    f"score={new_score:.1f} rank=#{new_rank} title={item.title}",
                )
                continue
            item.trakt_popularity_score = new_score
            item.trakt_popularity_rank = new_rank
            Item.objects.filter(pk=item.pk).update(
                trakt_popularity_score=new_score,
                trakt_popularity_rank=new_rank,
            )
            updated += 1

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete: {len(items)} item(s) matched."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Recomputed scores for {updated} item(s)."))

    def _parse_media_types(self, raw_value: str) -> list[str]:
        supported = {
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
        }
        values = [value.strip() for value in str(raw_value or "").split(",") if value.strip()]
        parsed = [value for value in values if value in supported]
        return parsed or sorted(supported)
