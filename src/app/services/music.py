"""Music-related service functions for discography sync."""

import logging
import re
from collections import defaultdict

from django.conf import settings
from django.db import IntegrityError, models
from django.utils import timezone
from django.utils.dateparse import parse_date

from app.log_safety import exception_summary
from app.models import Album, Artist, Track

logger = logging.getLogger(__name__)


def get_artist_hero_image(artist: Artist) -> str:
    """Get a hero image for an artist from their albums.
    
    Since MusicBrainz doesn't have artist photos, we derive a hero image
    from the artist's albums - preferring albums with cover art.
    
    Strategy:
    1. Find albums with images, prefer earliest release (often most iconic)
    2. If no albums have images, return the default placeholder
    
    Args:
        artist: The Artist object
        
    Returns:
        URL to the hero image, or settings.IMG_NONE
    """
    # Get all albums for this artist that have images
    albums_with_images = Album.objects.filter(
        artist=artist,
    ).exclude(
        image="",
    ).exclude(
        image=settings.IMG_NONE,
    ).order_by("release_date")

    if albums_with_images.exists():
        # Return the earliest album's image (often the most iconic)
        return albums_with_images.first().image

    return settings.IMG_NONE


def _norm_name(val: str) -> str:
    """Normalize names for matching (strip punctuation/whitespace, lowercase)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (val or "")).strip()).lower()


def resolve_artist_mbid(name: str, sort_name: str | None = None):
    """Resolve an artist MBID using the same heuristics as the app search.

    Returns (mbid, candidate_count, matched_variant) or (None, 0, None).
    """
    if not (name or sort_name):
        return None, 0, None

    from app.providers import musicbrainz

    # Limit to most likely variants for speed, but ensure we try multiple strategies
    variants = []
    base_names = {name} if name else set()
    if sort_name:
        base_names.add(sort_name)

    for base in base_names:
        # Most likely: exact name and quoted exact search
        variants.append(base)
        variants.append(f'"{base}"')
        # Try normalization variants if the name has special chars
        if "/" in base or "-" in base:
            variants.append(base.replace("/", " ").replace("-", " "))
            variants.append(f'"{base.replace("/", " ").replace("-", " ")}"')

    seen = set()
    variants_tried = []
    total_candidates_seen = 0
    for variant in variants:
        variant = variant.strip()
        if not variant or variant in seen:
            continue
        seen.add(variant)
        variants_tried.append(variant)

        try:
            resp = musicbrainz.search_artists(variant, page=1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.info(
                "resolve_artist_mbid: search failed for variant '%s': %s",
                variant,
                exc,
            )
            continue

        candidates = (resp or {}).get("artists") or (resp or {}).get("results") or []
        first_cand_name = candidates[0].get("name", "None") if candidates else "None"
        total_candidates_seen += len(candidates)

        logger.info(
            "resolve_artist_mbid: variant '%s' returned %d candidates (first: '%s')",
            variant,
            len(candidates),
            first_cand_name,
        )
        if not candidates:
            logger.info(
                "resolve_artist_mbid: variant '%s' returned no candidates, trying next variant",
                variant,
            )
            continue

        target_norm = _norm_name(variant)
        chosen = None
        is_exact_search = variant in base_names  # Unquoted exact name search

        logger.info(
            "resolve_artist_mbid: processing variant '%s' (is_exact_search=%s, target_norm='%s')",
            variant,
            is_exact_search,
            target_norm,
        )

        # PRIORITY 1: For exact unquoted name searches, trust MusicBrainz search ranking immediately
        # Manual searches work because users pick the first result - we should do the same
        if is_exact_search and candidates:
            first_cand = candidates[0]
            first_cand_id = first_cand.get("id")
            first_cand_name = first_cand.get("name", "Unknown")
            if first_cand_id:
                chosen = first_cand_id
                logger.info(
                    "resolve_artist_mbid: DECISION - using first candidate for exact search '%s' -> '%s' (MBID=%s, %d candidates, trusting MB search ranking)",
                    variant,
                    first_cand_name,
                    first_cand_id,
                    len(candidates),
                )
            else:
                logger.info(
                    "resolve_artist_mbid: exact search '%s' returned candidates but first has no ID, falling back to strict matching",
                    variant,
                )
        elif not is_exact_search:
            logger.info(
                "resolve_artist_mbid: variant '%s' is not exact search (quoted/normalized), using strict matching",
                variant,
            )
        elif not candidates:
            logger.info(
                "resolve_artist_mbid: exact search '%s' has no candidates, skipping",
                variant,
            )

        # PRIORITY 2: For quoted/normalized variants, use stricter matching
        if not chosen:
            logger.info(
                "resolve_artist_mbid: attempting strict matching for variant '%s'",
                variant,
            )
            # Try exact normalized match
            logger.info(
                "resolve_artist_mbid: trying exact normalized match for '%s' (target_norm='%s')",
                variant,
                target_norm,
            )
            exact_match_attempted = False
            for cand in candidates:
                cid = cand.get("id")
                cname = cand.get("name") or ""
                cand_norm = _norm_name(cname)
                exact_match_attempted = True
                if cid and cand_norm == target_norm:
                    chosen = cid
                    logger.info(
                        "resolve_artist_mbid: DECISION - exact normalized match '%s' -> '%s' (MBID=%s, norm='%s'=='%s')",
                        variant,
                        cname,
                        cid,
                        cand_norm,
                        target_norm,
                    )
                    break
            if not chosen and exact_match_attempted:
                logger.info(
                    "resolve_artist_mbid: exact normalized match failed for '%s', trying fuzzy match",
                    variant,
                )

            # Try fuzzy match (similar normalized names)
            if not chosen:
                logger.info(
                    "resolve_artist_mbid: trying fuzzy match for '%s' (target_norm='%s')",
                    variant,
                    target_norm,
                )
                fuzzy_match_attempted = False
                for cand in candidates:
                    cid = cand.get("id")
                    cname = cand.get("name") or ""
                    if not cid or not cname:
                        continue
                    fuzzy_match_attempted = True
                    cand_norm = _norm_name(cname)
                    # Allow match if normalized names are similar (80%+ similarity)
                    if cand_norm and target_norm:
                        # Simple similarity: check if one contains the other or vice versa
                        shorter = min(len(cand_norm), len(target_norm))
                        longer = max(len(cand_norm), len(target_norm))
                        contains_match = cand_norm in target_norm or target_norm in cand_norm
                        similarity_ratio = shorter / longer if longer > 0 else 0
                        if shorter > 0 and contains_match:
                            # Check length similarity
                            if similarity_ratio >= 0.8:
                                chosen = cid
                                logger.info(
                                    "resolve_artist_mbid: DECISION - fuzzy match '%s' -> '%s' (MBID=%s, norm: '%s'->'%s', similarity=%.2f)",
                                    variant,
                                    cname,
                                    cid,
                                    target_norm,
                                    cand_norm,
                                    similarity_ratio,
                                )
                                break
                            logger.info(
                                "resolve_artist_mbid: fuzzy match rejected for '%s' vs '%s' (similarity=%.2f < 0.8)",
                                variant,
                                cname,
                                similarity_ratio,
                            )
                if not chosen and fuzzy_match_attempted:
                    logger.info(
                        "resolve_artist_mbid: fuzzy match failed for '%s', trying case-insensitive match",
                        variant,
                    )

            # Try case-insensitive exact match
            if not chosen:
                logger.info(
                    "resolve_artist_mbid: trying case-insensitive match for '%s'",
                    variant,
                )
                case_match_attempted = False
                for cand in candidates:
                    cid = cand.get("id")
                    cname = cand.get("name") or ""
                    case_match_attempted = True
                    if cid and cname and cname.lower() == variant.lower():
                        chosen = cid
                        logger.info(
                            "resolve_artist_mbid: DECISION - case-insensitive match '%s' -> '%s' (MBID=%s)",
                            variant,
                            cname,
                            cid,
                        )
                        break
                if not chosen and case_match_attempted:
                    logger.info(
                        "resolve_artist_mbid: case-insensitive match failed for '%s', trying first candidate fallback",
                        variant,
                    )

            # Final fallback: use first candidate for non-exact searches
            if not chosen and candidates:
                first_cand = candidates[0]
                first_cand_id = first_cand.get("id")
                first_cand_name = first_cand.get("name", "Unknown")
                if first_cand_id:
                    # Trust first result if very few candidates (1-3) regardless of search type
                    if len(candidates) <= 3:
                        chosen = first_cand_id
                        logger.info(
                            "resolve_artist_mbid: DECISION - using first candidate for '%s' -> '%s' (MBID=%s, only %d candidates, trusting MB search ranking)",
                            variant,
                            first_cand_name,
                            first_cand_id,
                            len(candidates),
                        )
                    # Fallback: use first candidate but log as lower confidence
                    else:
                        chosen = first_cand_id
                        logger.info(
                            "resolve_artist_mbid: DECISION - using first candidate for '%s' -> '%s' (MBID=%s, no exact/fuzzy match, %d total candidates)",
                            variant,
                            first_cand_name,
                            first_cand_id,
                            len(candidates),
                        )
                else:
                    logger.info(
                        "resolve_artist_mbid: first candidate for '%s' has no ID, cannot use",
                        variant,
                    )

        if chosen:
            logger.info(
                "resolve_artist_mbid: SUCCESS - matched '%s' via variant '%s' -> MBID=%s",
                name or sort_name or "Unknown",
                variant,
                chosen,
            )
            return chosen, len(candidates), variant
        logger.info(
            "resolve_artist_mbid: no match found for variant '%s', trying next variant",
            variant,
        )

    logger.info(
        "resolve_artist_mbid: FAILED - no match found after trying %d variants: %s (total candidates seen: %d)",
        len(variants_tried),
        variants_tried,
        total_candidates_seen,
    )
    return None, 0, None


def resolve_album_mbid(album_title: str, artist_name: str | None = None):
    """Resolve an album MBID (release_group_id and release_id) using the same heuristics as resolve_artist_mbid.

    Returns (release_group_id, release_id, candidate_count, matched_variant) or (None, None, 0, None).
    """
    if not album_title:
        return None, None, 0, None

    from app.providers import musicbrainz

    # Build query variants - include artist name if available for better matching
    variants = []
    base_queries = []

    # Base query: just album title
    base_queries.append(album_title)

    # If artist name is available, add artist + album combinations
    if artist_name:
        base_queries.append(f"{artist_name} {album_title}")
        base_queries.append(f'"{artist_name}" "{album_title}"')

    for base in base_queries:
        # Most likely: exact name and quoted exact search
        variants.append(base)
        variants.append(f'"{base}"')
        # Try normalization variants if the name has special chars
        if "/" in base or "-" in base:
            variants.append(base.replace("/", " ").replace("-", " "))
            variants.append(f'"{base.replace("/", " ").replace("-", " ")}"')

    seen = set()
    variants_tried = []
    total_candidates_seen = 0
    for variant in variants:
        variant = variant.strip()
        if not variant or variant in seen:
            continue
        seen.add(variant)
        variants_tried.append(variant)

        try:
            resp = musicbrainz.search_releases(variant, page=1, skip_cover_art=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.info(
                "resolve_album_mbid: search failed for variant '%s': %s",
                variant,
                exc,
            )
            continue

        candidates = (resp or {}).get("results") or []
        first_cand_title = candidates[0].get("title", "None") if candidates else "None"
        total_candidates_seen += len(candidates)

        logger.info(
            "resolve_album_mbid: variant '%s' returned %d candidates (first: '%s')",
            variant,
            len(candidates),
            first_cand_title,
        )
        if not candidates:
            logger.info(
                "resolve_album_mbid: variant '%s' returned no candidates, trying next variant",
                variant,
            )
            continue

        target_norm = _norm_name(album_title)
        target_artist_norm = _norm_name(artist_name) if artist_name else None
        chosen_release_id = None
        chosen_release_group_id = None
        is_exact_search = variant in base_queries  # Unquoted exact search

        logger.info(
            "resolve_album_mbid: processing variant '%s' (is_exact_search=%s, target_norm='%s', artist_norm='%s')",
            variant,
            is_exact_search,
            target_norm,
            target_artist_norm or "None",
        )

        # PRIORITY 1: For exact unquoted name searches, trust MusicBrainz search ranking immediately
        if is_exact_search and candidates:
            first_cand = candidates[0]
            first_cand_release_id = first_cand.get("release_id")
            first_cand_title = first_cand.get("title", "Unknown")
            first_cand_artist = first_cand.get("artist_name", "")
            first_cand_artist_norm = _norm_name(first_cand_artist) if first_cand_artist else None

            # If artist name was provided, verify artist matches
            if target_artist_norm and first_cand_artist_norm:
                if first_cand_artist_norm != target_artist_norm:
                    logger.info(
                        "resolve_album_mbid: exact search '%s' first candidate artist '%s' doesn't match target '%s', falling back to strict matching",
                        variant,
                        first_cand_artist,
                        artist_name,
                    )
                elif first_cand_release_id:
                    # Artist matches, use first candidate
                    chosen_release_id = first_cand_release_id
                    logger.info(
                        "resolve_album_mbid: DECISION - using first candidate for exact search '%s' -> '%s' by '%s' (release_id=%s, %d candidates, trusting MB search ranking)",
                        variant,
                        first_cand_title,
                        first_cand_artist,
                        first_cand_release_id,
                        len(candidates),
                    )
            elif first_cand_release_id:
                # No artist provided or no artist in result, use first candidate
                chosen_release_id = first_cand_release_id
                logger.info(
                    "resolve_album_mbid: DECISION - using first candidate for exact search '%s' -> '%s' (release_id=%s, %d candidates, trusting MB search ranking)",
                    variant,
                    first_cand_title,
                    first_cand_release_id,
                    len(candidates),
                )
            else:
                logger.info(
                    "resolve_album_mbid: exact search '%s' returned candidates but first has no release_id, falling back to strict matching",
                    variant,
                )
        elif not is_exact_search:
            logger.info(
                "resolve_album_mbid: variant '%s' is not exact search (quoted/normalized), using strict matching",
                variant,
            )
        elif not candidates:
            logger.info(
                "resolve_album_mbid: exact search '%s' has no candidates, skipping",
                variant,
            )

        # PRIORITY 2: For quoted/normalized variants, use stricter matching
        if not chosen_release_id:
            logger.info(
                "resolve_album_mbid: attempting strict matching for variant '%s'",
                variant,
            )
            # Try exact normalized match
            logger.info(
                "resolve_album_mbid: trying exact normalized match for '%s' (target_norm='%s')",
                variant,
                target_norm,
            )
            exact_match_attempted = False
            for cand in candidates:
                cand_release_id = cand.get("release_id")
                cand_title = cand.get("title") or ""
                cand_artist = cand.get("artist_name") or ""
                cand_title_norm = _norm_name(cand_title)
                cand_artist_norm = _norm_name(cand_artist) if cand_artist else None
                exact_match_attempted = True

                # Check album title match
                title_matches = cand_title_norm == target_norm

                # If artist name was provided, also check artist match
                artist_matches = True
                if target_artist_norm:
                    if not cand_artist_norm:
                        artist_matches = False
                    else:
                        artist_matches = cand_artist_norm == target_artist_norm

                if cand_release_id and title_matches and artist_matches:
                    chosen_release_id = cand_release_id
                    logger.info(
                        "resolve_album_mbid: DECISION - exact normalized match '%s' -> '%s' by '%s' (release_id=%s, norm='%s'=='%s', artist_match=%s)",
                        variant,
                        cand_title,
                        cand_artist,
                        cand_release_id,
                        cand_title_norm,
                        target_norm,
                        artist_matches,
                    )
                    break
            if not chosen_release_id and exact_match_attempted:
                logger.info(
                    "resolve_album_mbid: exact normalized match failed for '%s', trying fuzzy match",
                    variant,
                )

            # Try fuzzy match (similar normalized names)
            if not chosen_release_id:
                logger.info(
                    "resolve_album_mbid: trying fuzzy match for '%s' (target_norm='%s')",
                    variant,
                    target_norm,
                )
                fuzzy_match_attempted = False
                for cand in candidates:
                    cand_release_id = cand.get("release_id")
                    cand_title = cand.get("title") or ""
                    cand_artist = cand.get("artist_name") or ""
                    if not cand_release_id or not cand_title:
                        continue
                    fuzzy_match_attempted = True
                    cand_title_norm = _norm_name(cand_title)
                    cand_artist_norm = _norm_name(cand_artist) if cand_artist else None

                    # Check title similarity
                    if cand_title_norm and target_norm:
                        shorter = min(len(cand_title_norm), len(target_norm))
                        longer = max(len(cand_title_norm), len(target_norm))
                        contains_match = cand_title_norm in target_norm or target_norm in cand_title_norm
                        similarity_ratio = shorter / longer if longer > 0 else 0

                        # Check artist match if artist name was provided
                        artist_matches = True
                        if target_artist_norm:
                            if not cand_artist_norm:
                                artist_matches = False
                            else:
                                artist_matches = cand_artist_norm == target_artist_norm

                        if shorter > 0 and contains_match and similarity_ratio >= 0.8 and artist_matches:
                            chosen_release_id = cand_release_id
                            logger.info(
                                "resolve_album_mbid: DECISION - fuzzy match '%s' -> '%s' by '%s' (release_id=%s, norm: '%s'->'%s', similarity=%.2f, artist_match=%s)",
                                variant,
                                cand_title,
                                cand_artist,
                                cand_release_id,
                                target_norm,
                                cand_title_norm,
                                similarity_ratio,
                                artist_matches,
                            )
                            break
                        if shorter > 0 and contains_match and similarity_ratio >= 0.8:
                            logger.info(
                                "resolve_album_mbid: fuzzy match rejected for '%s' vs '%s' (similarity=%.2f >= 0.8 but artist mismatch: '%s' vs '%s')",
                                variant,
                                cand_title,
                                similarity_ratio,
                                target_artist_norm,
                                cand_artist_norm,
                            )
                if not chosen_release_id and fuzzy_match_attempted:
                    logger.info(
                        "resolve_album_mbid: fuzzy match failed for '%s', trying case-insensitive match",
                        variant,
                    )

            # Try case-insensitive exact match
            if not chosen_release_id:
                logger.info(
                    "resolve_album_mbid: trying case-insensitive match for '%s'",
                    variant,
                )
                case_match_attempted = False
                for cand in candidates:
                    cand_release_id = cand.get("release_id")
                    cand_title = cand.get("title") or ""
                    cand_artist = cand.get("artist_name") or ""
                    case_match_attempted = True

                    title_matches = cand_release_id and cand_title and cand_title.lower() == album_title.lower()
                    artist_matches = True
                    if artist_name and cand_artist:
                        artist_matches = cand_artist.lower() == artist_name.lower()

                    if title_matches and artist_matches:
                        chosen_release_id = cand_release_id
                        logger.info(
                            "resolve_album_mbid: DECISION - case-insensitive match '%s' -> '%s' by '%s' (release_id=%s)",
                            variant,
                            cand_title,
                            cand_artist,
                            cand_release_id,
                        )
                        break
                if not chosen_release_id and case_match_attempted:
                    logger.info(
                        "resolve_album_mbid: case-insensitive match failed for '%s', trying first candidate fallback",
                        variant,
                    )

            # Final fallback: use first candidate for non-exact searches
            if not chosen_release_id and candidates:
                first_cand = candidates[0]
                first_cand_release_id = first_cand.get("release_id")
                first_cand_title = first_cand.get("title", "Unknown")
                if first_cand_release_id:
                    # Trust first result if very few candidates (1-3) regardless of search type
                    if len(candidates) <= 3:
                        chosen_release_id = first_cand_release_id
                        logger.info(
                            "resolve_album_mbid: DECISION - using first candidate for '%s' -> '%s' (release_id=%s, only %d candidates, trusting MB search ranking)",
                            variant,
                            first_cand_title,
                            first_cand_release_id,
                            len(candidates),
                        )
                    # Fallback: use first candidate but log as lower confidence
                    else:
                        chosen_release_id = first_cand_release_id
                        logger.info(
                            "resolve_album_mbid: DECISION - using first candidate for '%s' -> '%s' (release_id=%s, no exact/fuzzy match, %d total candidates)",
                            variant,
                            first_cand_title,
                            first_cand_release_id,
                            len(candidates),
                        )
                else:
                    logger.info(
                        "resolve_album_mbid: first candidate for '%s' has no release_id, cannot use",
                        variant,
                    )

        # If we found a release_id, fetch the release to get release_group_id
        if chosen_release_id:
            try:
                # Get release_group_id from the release
                # We need to make a direct API call since get_release doesn't return release_group_id in the result dict
                # Import the private function for this specific use case
                from app.providers.musicbrainz import _mb_request
                try:
                    release_response = _mb_request(f"release/{chosen_release_id}", {"inc": "release-groups"})
                    release_group = release_response.get("release-group", {})
                    chosen_release_group_id = release_group.get("id")
                except Exception as e:
                    logger.debug("Failed to get release_group_id for release %s: %s", chosen_release_id, e)
                    chosen_release_group_id = None

                logger.info(
                    "resolve_album_mbid: SUCCESS - matched '%s' via variant '%s' -> release_id=%s, release_group_id=%s",
                    album_title,
                    variant,
                    chosen_release_id,
                    chosen_release_group_id or "None",
                )
                return chosen_release_group_id, chosen_release_id, len(candidates), variant
            except Exception as e:
                logger.warning(
                    "resolve_album_mbid: Failed to fetch release data for release_id %s: %s",
                    chosen_release_id,
                    e,
                )
                # Return release_id even without release_group_id
                return None, chosen_release_id, len(candidates), variant
        else:
            logger.info(
                "resolve_album_mbid: no match found for variant '%s', trying next variant",
                variant,
            )

    logger.info(
        "resolve_album_mbid: FAILED - no match found after trying %d variants: %s (total candidates seen: %d)",
        len(variants_tried),
        variants_tried,
        total_candidates_seen,
    )
    return None, None, 0, None


def refresh_album_cover_art(album: Album) -> bool:
    """Try to fetch/refresh cover art for an album.
    
    Args:
        album: The Album object to refresh
        
    Returns:
        True if cover art was updated, False otherwise
    """
    from app.providers import musicbrainz

    # Only try if we have IDs to look up
    if not album.musicbrainz_release_id and not album.musicbrainz_release_group_id:
        return False

    # Skip if album already has good cover art
    if album.image and album.image != settings.IMG_NONE:
        return False

    try:
        new_image = musicbrainz.get_cover_art(
            release_id=album.musicbrainz_release_id,
            release_group_id=album.musicbrainz_release_group_id,
        )

        if new_image and new_image != settings.IMG_NONE:
            album.image = new_image
            album.save(update_fields=["image"])
            logger.info("Updated cover art for album %s", album.title)
            return True
    except Exception as e:
        logger.debug("Failed to fetch cover art for album %s: %s", album.title, e)

    # Try iTunes as fallback if MusicBrainz didn't find artwork
    if album.image == settings.IMG_NONE or not album.image:
        if album.artist and album.title:
            try:
                from integrations import itunes_music_artwork
                itunes_image = itunes_music_artwork.fetch_album_artwork(
                    album_title=album.title,
                    artist_name=album.artist.name,
                )
                if itunes_image:
                    album.image = itunes_image
                    album.save(update_fields=["image"])
                    logger.info("Updated cover art for album %s from iTunes", album.title)
                    return True
            except Exception as e:
                logger.debug("Failed to fetch cover art from iTunes for album %s: %s", album.title, e)

    return False


def refresh_missing_album_covers(artist: Artist, limit: int = 10) -> int:
    """Refresh cover art for albums missing images.
    
    Args:
        artist: The Artist whose albums to check
        limit: Maximum number of albums to refresh (to avoid rate limiting)
        
    Returns:
        Number of albums that got new cover art
    """
    albums_without_images = Album.objects.filter(
        artist=artist,
    ).filter(
        models.Q(image="") | models.Q(image=settings.IMG_NONE),
    )[:limit]

    refreshed = 0
    for album in albums_without_images:
        if refresh_album_cover_art(album):
            refreshed += 1

    return refreshed


def sync_artist_discography(artist: Artist, force: bool = False) -> int:
    """Sync the discography for an artist from MusicBrainz.
    
    This creates/updates Album records for all albums in the artist's
    discography, similar to how TV seasons are populated from TMDB.
    
    Args:
        artist: The Artist object to sync
        force: If True, sync even if already synced recently
        
    Returns:
        Number of albums synced
    """
    from app.providers import musicbrainz

    # Ensure artist is saved before using it in queries
    if not artist.pk:
        artist.save()

    # Skip if no MusicBrainz ID
    if not artist.musicbrainz_id:
        logger.debug("Artist %s has no MusicBrainz ID, skipping discography sync", artist.name)
        return 0

    # Skip if already synced recently unless forced
    # More aggressive skipping: 30 days if albums exist, 7 days if no albums yet
    if not force and artist.discography_synced_at:
        days_since_sync = (timezone.now() - artist.discography_synced_at).days
        existing_albums = Album.objects.filter(artist=artist).exists()
        max_age = 30 if existing_albums else 7  # Longer skip if we have albums

        if days_since_sync < max_age:
            logger.debug(
                "Artist %s discography synced %d days ago (max_age=%d, has_albums=%s), skipping",
                artist.name,
                days_since_sync,
                max_age,
                existing_albums,
            )
            return 0

    try:
        # Skip cover art fetching during sync - covers are loaded async via HTMX
        discography = musicbrainz.get_artist_discography(artist.musicbrainz_id, skip_cover_art=True)

        synced_count = 0
        for album_data in discography:
            release_group_id = album_data.get("release_group_id")
            if not release_group_id:
                continue

            # Parse release date
            release_date = None
            date_str = album_data.get("release_date", "")
            if date_str:
                try:
                    if len(date_str) >= 10:
                        release_date = parse_date(date_str[:10])
                    elif len(date_str) == 7:
                        release_date = parse_date(date_str + "-01")
                    elif len(date_str) == 4:
                        release_date = parse_date(date_str + "-01-01")
                except (ValueError, TypeError):
                    pass

            # Update or create the album
            album, created = Album.objects.update_or_create(
                artist=artist,
                musicbrainz_release_group_id=release_group_id,
                defaults={
                    "title": album_data.get("title", "Unknown Album"),
                    "musicbrainz_release_id": album_data.get("release_id"),
                    "release_date": release_date,
                    "image": album_data.get("image", ""),
                    "release_type": album_data.get("release_type", ""),
                },
            )

            if created:
                logger.debug("Created album: %s", album.title)
            else:
                logger.debug("Updated album: %s", album.title)

            synced_count += 1

        # Update sync timestamp
        artist.discography_synced_at = timezone.now()
        artist.save(update_fields=["discography_synced_at"])

        logger.info(
            "Synced %d albums for artist %s from MusicBrainz",
            synced_count,
            artist.name,
        )
        return synced_count

    except Exception as e:
        logger.exception("Failed to sync discography for artist %s: %s", artist.name, e)
        return 0


def needs_discography_sync(artist: Artist, max_age_days: int = 7) -> bool:
    """Check if an artist needs discography sync.
    
    Args:
        artist: The Artist object to check
        max_age_days: Maximum age of sync before it's considered stale
        
    Returns:
        True if sync is needed
    """
    if not artist.musicbrainz_id:
        return False

    if not artist.discography_synced_at:
        return True

    days_since_sync = (timezone.now() - artist.discography_synced_at).days
    return days_since_sync >= max_age_days


_DISCOGRAPHY_PRIMARY_ORDER = {
    "Album": 0,
    "EP": 1,
    "Single": 2,
    "Broadcast": 3,
    "Other": 4,
    "Unknown": 5,
}
_DISCOGRAPHY_SECONDARY_ORDER = {
    "Compilation": 0,
    "Mixtape/Street": 1,
    "Soundtrack": 2,
    "Remix": 3,
    "Live": 4,
}


def _split_release_type(release_type: str | None) -> tuple[str, list[str], str]:
    if not release_type:
        return "Unknown", [], "Unknown"

    parts = [part.strip() for part in release_type.split(" + ", 1)]
    primary = parts[0] or "Unknown"
    secondary = []
    if len(parts) > 1:
        secondary = [part.strip() for part in parts[1].split(",") if part.strip()]

    label = release_type
    if primary == "Compilation" and not secondary:
        primary = "Album"
        secondary = ["Compilation"]
        label = "Album + Compilation"
    elif primary == "Other" and secondary:
        label = ", ".join(secondary)

    return primary, secondary, label


def build_discography_groups(albums: list[Album]) -> list[dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"albums": [], "primary": "Unknown", "secondary": []})
    for album in albums:
        primary, secondary, label = _split_release_type(album.release_type)
        entry = groups[label]
        entry["albums"].append(album)
        entry["primary"] = primary
        entry["secondary"] = secondary

    def sort_key(item: tuple[str, dict]) -> tuple:
        label, data = item
        primary = data["primary"]
        secondary = data["secondary"]
        primary_order = _DISCOGRAPHY_PRIMARY_ORDER.get(primary, len(_DISCOGRAPHY_PRIMARY_ORDER))
        secondary_order = tuple(_DISCOGRAPHY_SECONDARY_ORDER.get(sec, 100) for sec in secondary)
        return (primary_order, 0 if not secondary else 1, secondary_order, label.lower())

    grouped = []
    for label, data in sorted(groups.items(), key=sort_key):
        grouped.append({
            "label": label,
            "albums": data["albums"],
            "count": len(data["albums"]),
        })

    return grouped


def ensure_album_has_release_id(album: Album) -> bool:
    """Ensure an album has a release_id, fetching it from release_group if needed.
    
    If the album only has a release_group_id, this will query MusicBrainz to find
    a representative release and update the album.
    
    Args:
        album: The Album object
        
    Returns:
        True if the album now has a release_id (or already had one)
    """
    from app.providers import musicbrainz

    # Already has a release_id
    if album.musicbrainz_release_id:
        return True

    # No release_group_id either - truly no MusicBrainz identity
    if not album.musicbrainz_release_group_id:
        return False

    # Try to get a release from the release group
    try:
        release_id = musicbrainz.get_release_for_group(album.musicbrainz_release_group_id)
        if release_id:
            album.musicbrainz_release_id = release_id
            album.save(update_fields=["musicbrainz_release_id"])
            logger.info("Found release_id %s for album %s", release_id, album.title)
            return True
    except Exception as e:
        logger.debug("Failed to get release_id for album %s: %s", album.title, e)

    return False


def album_has_musicbrainz_id(album: Album) -> bool:
    """Check if an album has any MusicBrainz identity.
    
    Returns True if the album has either a release_id or release_group_id.
    """
    return bool(album.musicbrainz_release_id or album.musicbrainz_release_group_id)


def populate_album_tracks(album: Album) -> int:
    """Populate Track rows for an album from MusicBrainz and mark tracks_populated."""
    from app.providers import musicbrainz

    if album.tracks_populated:
        return 0

    # Ensure we have a concrete release_id (release_group alone can't list tracks)
    if not album.musicbrainz_release_id:
        ensure_album_has_release_id(album)
    if not album.musicbrainz_release_id:
        return 0

    try:
        release_data = musicbrainz.get_release(album.musicbrainz_release_id)
        tracks_data = release_data.get("tracks", [])

        # Update genres from release if album lacks them
        if release_data.get("genres") and not album.genres:
            album.genres = release_data.get("genres")

        created_or_updated = 0
        for track_data in tracks_data:
            _, created = Track.objects.update_or_create(
                album=album,
                disc_number=track_data.get("disc_number", 1),
                track_number=track_data.get("track_number"),
                defaults={
                    "title": track_data.get("title", "Unknown Track"),
                    "musicbrainz_recording_id": track_data.get("recording_id"),
                    "duration_ms": track_data.get("duration_ms"),
                    "genres": track_data.get("genres", []) or release_data.get("genres", []),
                },
            )
            if created:
                created_or_updated += 1

        # Also update album image if missing
        if (not album.image or album.image == settings.IMG_NONE) and release_data.get("image"):
            album.image = release_data["image"]

        album.tracks_populated = True
        album.save(update_fields=["tracks_populated", "image", "genres"])
        logger.info("Populated %d tracks for album %s", len(tracks_data), album.title)
        return len(tracks_data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to populate tracks for album %s: %s", album.title, exception_summary(exc))
        return 0


def prefetch_album_covers(artist: Artist, limit: int | None = 20) -> int:
    """Prefetch cover art for albums missing images.
    
    This runs on artist page load to populate album covers that
    were not fetched during discography sync.
    
    Args:
        artist: The Artist whose albums to check
        limit: Maximum number of albums to prefetch (to respect rate limits)
        
    Returns:
        Number of albums that got new cover art
    """
    from app.providers import musicbrainz

    # Find albums with missing images (iTunes fallback does not require MBIDs)
    albums_qs = Album.objects.filter(
        artist=artist,
    ).filter(
        models.Q(image="") | models.Q(image=settings.IMG_NONE),
    )

    albums_needing_art = albums_qs[:limit] if limit else albums_qs

    updated = 0
    for album in albums_needing_art:
        try:
            if album.musicbrainz_release_id or album.musicbrainz_release_group_id:
                image = musicbrainz.get_cover_art(
                    release_id=album.musicbrainz_release_id,
                    release_group_id=album.musicbrainz_release_group_id,
                )
                if image and image != settings.IMG_NONE:
                    album.image = image
                    album.save(update_fields=["image"])
                    updated += 1
                    logger.debug("Prefetched cover for album: %s", album.title)
                    continue  # Skip iTunes fallback if MusicBrainz succeeded
        except Exception as e:
            logger.debug("Failed to prefetch cover for %s: %s", album.title, e)

        # Try iTunes as fallback if MusicBrainz didn't find artwork
        if album.image == settings.IMG_NONE or not album.image:
            if album.title:
                try:
                    from integrations import itunes_music_artwork
                    artist_name = album.artist.name if album.artist else artist.name
                    itunes_image = itunes_music_artwork.fetch_album_artwork(
                        album_title=album.title,
                        artist_name=artist_name,
                    )
                    if itunes_image:
                        album.image = itunes_image
                        album.save(update_fields=["image"])
                        updated += 1
                        logger.debug("Prefetched cover for album %s from iTunes", album.title)
                except Exception as e:
                    logger.debug("Failed to prefetch cover from iTunes for %s: %s", album.title, e)

    return updated


def _preferred_status(current: str | None, incoming: str | None) -> str | None:
    """Pick the higher-precedence status between two values."""
    from app.models import Status

    if not current:
        return incoming
    if not incoming:
        return current

    order = {
        Status.COMPLETED.value: 5,
        Status.DROPPED.value: 4,
        Status.IN_PROGRESS.value: 3,
        Status.PAUSED.value: 2,
        Status.PLANNING.value: 1,
    }
    return current if order.get(current, 0) >= order.get(incoming, 0) else incoming


def merge_artist_records(source_artist: Artist, target_artist: Artist) -> Artist:
    """Merge a duplicate artist into a canonical one without losing data."""
    if source_artist.id == target_artist.id:
        return target_artist

    from app.models import (
        Album,
        AlbumTracker,
        ArtistTracker,
        Music,
    )
    from app.services.music_scrobble import dedupe_artist_albums

    # Merge artist trackers (per-user status/score)
    for tracker in ArtistTracker.objects.filter(artist=source_artist):
        existing = ArtistTracker.objects.filter(
            user=tracker.user,
            artist=target_artist,
        ).first()

        if existing:
            updates = set()
            preferred_status = _preferred_status(existing.status, tracker.status)
            if preferred_status and preferred_status != existing.status:
                existing.status = preferred_status
                updates.add("status")

            # Preserve earliest start and latest end dates
            start_date = min(
                [d for d in [existing.start_date, tracker.start_date] if d],
                default=None,
            )
            end_date = max(
                [d for d in [existing.end_date, tracker.end_date] if d],
                default=None,
            )
            if start_date and start_date != existing.start_date:
                existing.start_date = start_date
                updates.add("start_date")
            if end_date and end_date != existing.end_date:
                existing.end_date = end_date
                updates.add("end_date")

            # Fill missing score/notes
            if existing.score is None and tracker.score is not None:
                existing.score = tracker.score
                updates.add("score")
            if tracker.notes and tracker.notes.strip():
                if not existing.notes:
                    existing.notes = tracker.notes
                    updates.add("notes")
                elif tracker.notes not in existing.notes:
                    existing.notes = f"{existing.notes}\n{tracker.notes}"
                    updates.add("notes")

            if updates:
                existing.save(update_fields=list(updates))
            tracker.delete()
        else:
            tracker.artist = target_artist
            tracker.save(update_fields=["artist"])

    def _merge_album_into_target(source_album: Album, target_album: Album):
        updates = set()
        if (
            (not target_album.image or target_album.image == settings.IMG_NONE)
            and source_album.image
            and source_album.image != settings.IMG_NONE
        ):
            target_album.image = source_album.image
            updates.add("image")
        if not target_album.musicbrainz_release_id and source_album.musicbrainz_release_id:
            target_album.musicbrainz_release_id = source_album.musicbrainz_release_id
            updates.add("musicbrainz_release_id")
        if not target_album.musicbrainz_release_group_id and source_album.musicbrainz_release_group_id:
            # Check for conflicts before setting - another album on this artist might already have this release_group_id
            conflict = Album.objects.filter(
                artist=target_album.artist,
                musicbrainz_release_group_id=source_album.musicbrainz_release_group_id,
            ).exclude(id=target_album.id).first()
            if not conflict:
                target_album.musicbrainz_release_group_id = source_album.musicbrainz_release_group_id
                updates.add("musicbrainz_release_group_id")
            else:
                logger.debug(
                    "Skipping musicbrainz_release_group_id merge from '%s' (id=%s) to '%s' (id=%s): "
                    "conflicts with album '%s' (id=%s) for artist %s",
                    source_album.title,
                    source_album.id,
                    target_album.title,
                    target_album.id,
                    conflict.title,
                    conflict.id,
                    target_album.artist.name if target_album.artist else "Unknown",
                )
        if not target_album.release_date and source_album.release_date:
            target_album.release_date = source_album.release_date
            updates.add("release_date")
        if not target_album.release_type and source_album.release_type:
            target_album.release_type = source_album.release_type
            updates.add("release_type")
        if updates:
            try:
                target_album.save(update_fields=list(updates))
            except IntegrityError as e:
                logger.warning(
                    "Failed to merge album '%s' (id=%s) into '%s' (id=%s): %s. "
                    "Skipping metadata merge.",
                    source_album.title,
                    source_album.id,
                    target_album.title,
                    target_album.id,
                    e,
                )
                target_album.refresh_from_db()

        # Merge album trackers
        for tracker in AlbumTracker.objects.filter(album=source_album):
            existing = AlbumTracker.objects.filter(
                user=tracker.user,
                album=target_album,
            ).first()
            if existing:
                tracker_updates = set()
                preferred_status = _preferred_status(existing.status, tracker.status)
                if preferred_status and preferred_status != existing.status:
                    existing.status = preferred_status
                    tracker_updates.add("status")

                start_date = min(
                    [d for d in [existing.start_date, tracker.start_date] if d],
                    default=None,
                )
                end_date = max(
                    [d for d in [existing.end_date, tracker.end_date] if d],
                    default=None,
                )
                if start_date and start_date != existing.start_date:
                    existing.start_date = start_date
                    tracker_updates.add("start_date")
                if end_date and end_date != existing.end_date:
                    existing.end_date = end_date
                    tracker_updates.add("end_date")

                if existing.score is None and tracker.score is not None:
                    existing.score = tracker.score
                    tracker_updates.add("score")

                if tracker_updates:
                    existing.save(update_fields=list(tracker_updates))
                tracker.delete()
            else:
                tracker.album = target_album
                tracker.save(update_fields=["album"])

        # Re-point music entries to the canonical album
        Music.objects.filter(album=source_album).update(album=target_album, track=None)

        # Dropping the source album will also drop its tracks; music.track is SET_NULL
        source_album.delete()

    # Move albums over; if a collision happens, merge then delete source
    for album in Album.objects.filter(artist=source_artist):
        album.artist = target_artist
        try:
            album.save(update_fields=["artist"])
            continue
        except IntegrityError:
            conflict = (
                Album.objects.filter(
                    artist=target_artist,
                    musicbrainz_release_group_id=album.musicbrainz_release_group_id,
                ).first()
                or Album.objects.filter(
                    artist=target_artist,
                    musicbrainz_release_id=album.musicbrainz_release_id,
                ).first()
                or Album.objects.filter(
                    artist=target_artist,
                    title=album.title,
                ).first()
            )
            if conflict:
                _merge_album_into_target(album, conflict)
            else:
                # As a last resort, drop the conflicting album to avoid blocking merge
                Music.objects.filter(album=album).update(album=None, track=None)
                album.delete()

    # Move orphaned music entries that reference the artist directly
    Music.objects.filter(artist=source_artist).update(artist=target_artist)

    # Clean up the old artist now that references are moved
    source_artist.delete()

    # Final dedupe pass to collapse any remaining duplicate albums for the target artist
    dedupe_artist_albums(target_artist)

    return target_artist


def link_music_to_tracks(user, limit: int | None = None):
    """Link Music entries to Track models by matching recording IDs.
    
    This cleanup function helps fix Music entries that weren't linked to Track models
    during import or enrichment.
    
    Args:
        user: Django User instance
        limit: Optional limit on number of entries to process
        
    Returns:
        dict with counts of linked entries
    """
    from app.models import Music, Track
    from app.services.music_scrobble import _runtime_minutes_from_ms

    # Find Music entries without Track links but with recording IDs
    music_entries = Music.objects.filter(
        user=user,
        track__isnull=True,
        item__media_id__isnull=False,
    ).select_related("item", "album")

    if limit:
        music_entries = music_entries[:limit]

    linked_count = 0
    runtime_backfilled = 0

    for music in music_entries:
        recording_id = music.item.media_id if music.item else None

        if not recording_id:
            continue

        # Try to find a Track with matching recording ID
        # First check tracks from the same album if available
        if music.album_id:
            track = Track.objects.filter(
                album=music.album,
                musicbrainz_recording_id=recording_id,
            ).first()

            if track:
                music.track = track
                music.save(update_fields=["track"])
                linked_count += 1

                # Backfill runtime if track has duration
                if track.duration_ms and music.item and not music.item.runtime_minutes:
                    runtime = _runtime_minutes_from_ms(track.duration_ms)
                    if runtime:
                        music.item.runtime_minutes = runtime
                        music.item.save(update_fields=["runtime_minutes"])
                        runtime_backfilled += 1
                continue

        # Try broader search across all albums for this artist
        if music.artist_id:
            track = Track.objects.filter(
                album__artist_id=music.artist_id,
                musicbrainz_recording_id=recording_id,
            ).first()

            if track:
                music.track = track
                # Also link to the correct album if not already linked
                if not music.album_id:
                    music.album = track.album
                    music.save(update_fields=["track", "album"])
                else:
                    music.save(update_fields=["track"])
                linked_count += 1

                # Backfill runtime
                if track.duration_ms and music.item and not music.item.runtime_minutes:
                    runtime = _runtime_minutes_from_ms(track.duration_ms)
                    if runtime:
                        music.item.runtime_minutes = runtime
                        music.item.save(update_fields=["runtime_minutes"])
                        runtime_backfilled += 1

    logger.info(
        "link_music_to_tracks: Linked %d Music entries to tracks, backfilled %d runtimes",
        linked_count,
        runtime_backfilled,
    )

    return {
        "linked": linked_count,
        "runtime_backfilled": runtime_backfilled,
    }


def backfill_music_runtimes(user, limit: int | None = None):
    """Backfill missing runtime data for Music entries from Track durations.
    
    Args:
        user: Django User instance
        limit: Optional limit on number of entries to process
        
    Returns:
        dict with count of backfilled runtimes
    """
    from app.models import Music
    from app.services.music_scrobble import _runtime_minutes_from_ms

    music_entries = Music.objects.filter(
        user=user,
        item__runtime_minutes__isnull=True,
        track__duration_ms__isnull=False,
    ).select_related("item", "track")

    if limit:
        music_entries = music_entries[:limit]

    backfilled = 0

    for music in music_entries:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                music.item.save(update_fields=["runtime_minutes"])
                backfilled += 1

    logger.info("backfill_music_runtimes: Backfilled %d runtimes", backfilled)

    return {"backfilled": backfilled}


def fix_music_album_links(user, limit: int | None = None):
    """Fix Music entries missing album links by inferring from Track or Artist.
    
    Args:
        user: Django User instance
        limit: Optional limit on number of entries to process
        
    Returns:
        dict with count of fixed links
    """
    from app.models import Music

    music_entries = Music.objects.filter(
        user=user,
        album__isnull=True,
    ).select_related("track", "artist")

    if limit:
        music_entries = music_entries[:limit]

    fixed_count = 0

    for music in music_entries:
        # Try to infer album from track
        if music.track and music.track.album_id:
            music.album = music.track.album
            if not music.artist_id and music.track.album.artist_id:
                music.artist = music.track.album.artist
            music.save(update_fields=["album", "artist"] if not music.artist_id else ["album"])
            fixed_count += 1
            continue

        # Try to infer album from artist (pick first album for now - not ideal but better than null)
        if music.artist_id:
            first_album = Album.objects.filter(artist=music.artist).first()
            if first_album:
                music.album = first_album
                music.save(update_fields=["album"])
                fixed_count += 1

    logger.info("fix_music_album_links: Fixed %d album links", fixed_count)

    return {"fixed": fixed_count}
