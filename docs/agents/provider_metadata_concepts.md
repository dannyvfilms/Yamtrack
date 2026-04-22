# Provider and Metadata Concepts

This document explains the different notions of providers, sources, and metadata in Yamtrack. Understanding these distinctions is crucial for debugging display issues, implementing new features, or understanding how cross-provider lookups work.

## Core Terminology

### 1. Identity Provider (Tracking Source)

The **identity provider** (also called `source` or `tracking source`) is the **original provider where you added/tracked a title**. This is immutable after creation and determines:

- Which provider's ID your progress/history are tied to
- The URL route structure (`/tv/{id}/show/tmdb` - the `tmdb` at the end)
- Where episode/season ordering comes from by default
- How the item appears in lists filtered by source

Stored in [`Item.source`](src/app/models.py:85), this field is part of the item's identity tuple `(media_type, source, media_id)`.

**Example**: If you add "Breaking Bad" via TMDB, then `Item.source = "tmdb"` and `Item.media_id = "1396"`.

### 2. Display Provider

The **display provider** is which provider's metadata you want to **see on detail pages**. This is a user preference that can be changed per-item or globally for TV/Anime types.

Configured via Preferences → Metadata Providers:
- User-level defaults: `tv_metadata_source_default`, `anime_metadata_source_default`
- Per-item overrides: stored in [`MetadataProviderPreference`](src/app/models.py:764)

**Example**: Track on TMDB but prefer TVDB's episode titles and images.

### 3. Route Source

The **route source** is the `source` parameter extracted from the URL. For most requests, this equals the identity provider, but it can differ when viewing public profiles or shared links.

URL pattern: `{source}/{media_type}/{media_id}/{slug}/{source}`
- `/tv/1396/breaking-bad/tmdb` → route source = `tmdb`
- `/anime/21/one-piece/mal` → route source = `mal`

See [`src/app/views.py:4390`](src/app/views.py:4390) for `media_details` view routing.

## How They Interact

### Default Behavior (No Override)

When no display provider override exists:

```python
# From src/app/services/metadata_resolution.py:228-232
identity_provider = item.source  # Tracking source
provider = metadata_default_source(user, route_media_type)  # Defaults to identity_provider
```

Result: `display_provider == identity_provider == route_source`

### With Display Provider Override

When a user sets a different display provider:

```python
# From src/app/services/metadata_resolution.py:737-774
identity_provider = item.source if item else source  # Still tracking source
provider = get_preferred_provider(...)  # Could be different (from user prefs)

if provider != identity_provider:
    # Look up cross-provider ID
    provider_media_id = resolve_provider_media_id(item, provider, ...)
    
    # Fetch overlay metadata from display provider
    overlay_metadata = services.get_media_metadata(
        provider_route_media_type(...), provider_media_id, provider
    )
    
    # Merge: keep base (identity) structure, overlay display fields
    header_metadata = _overlay_header_metadata(base_metadata, overlay_metadata, provider)
```

**Important**: Only specific fields are overlaid (title, image, synopsis, genres, score). The `related.seasons` array and episode structures come from the **identity provider**, not the display provider.

See the overlay logic in [`src/app/services/metadata_resolution.py:452-465`](src/app/services/metadata_resolution.py:452):

```python
merged = dict(base_metadata)  # base = identity provider
for key in (
    "title", "original_title", "localized_title", 
    "image", "synopsis", "genres", "score", "score_count", "tvdb_id",
):
    if key in overlay_metadata and overlay_metadata.get(key) not in (None, ""):
        merged[key] = overlay_metadata[key]  # overlay = display provider

merged["display_source"] = provider
merged["display_source_url"] = overlay_metadata.get("source_url")
```

Notice `related.seasons` and `episodes` are **not** in this list - they stay as the original identity provider data.

## Episode/Season Ordering

**Episodes and seasons are ordered by the identity provider**, not the display provider.

From [`src/app/views.py:4997`](src/app/views.py:4997):

```python
media_metadata = services.get_media_metadata(media_type, media_id, source)
```

The `source` here is the **route source** (typically the identity provider), so:

- Episodes/seasons fetched from `Item.source`'s API
- Season numbers, episode numbers, air dates from identity provider
- Display provider only affects visual fields (titles, images, synopses)

### Why This Design?

Episode ordering is tied to tracking source because:
1. Progress history references episode IDs from the identity provider
2. Cross-provider mapping ensures consistency between what you tracked and what you see
3. Prevents confusion when changing display preferences mid-watch

### Getting Different Episode Ordering

To see episodes from a different provider's ordering:
1. **Migrate the title** to change `Item.source` permanently (separate action)
2. Or use a different route with a different `source` parameter

See [`src/app/services/anime_migration.py:108-110`](src/app/services/anime_migration.py:108) for migration logic.

## Cross-Provider Mapping

When display provider differs from identity provider, Yamtrack uses stored external IDs to map between providers.

### Storage

[`ItemProviderLink`](src/app/models.py:764) stores cross-provider mappings:

```python
class ItemProviderLink(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    provider = models.CharField(max_length=20, choices=Sources)
    provider_media_id = models.CharField(max_length=20)
    provider_media_type = models.CharField(max_length=10)
    season_number = models.PositiveIntegerField(null=True, blank=True)
```

Populated by [`upsert_provider_links()`](src/app/services/metadata_resolution.py:304) when fetching metadata.

### Resolution

[`resolve_provider_media_id()`](src/app/services/metadata_resolution.py:395) looks up the display provider's ID using:
1. Stored `ItemProviderLink` records
2. Fallback to `item.provider_external_ids` JSON field
3. Special handling for grouped anime (MAL ↔ TMDB/TVDB mapping)

See [`src/app/providers/services.py:315`](src/app/providers/services.py:315) for `get_media_metadata` routing.

## Sources Enum

All providers are defined in [`src/app/models.py:46-61`](src/app/models.py:46):

```python
class Sources(models.TextChoices):
    TMDB = "tmdb", "The Movie Database"
    TVDB = "tvdb", "TheTVDB"
    MAL = "mal", "MyAnimeList"
    MANGAUPDATES = "mangaupdates", "MangaUpdates"
    IGDB = "igdb", "Internet Game Database"
    OPENLIBRARY = "openlibrary", "Open Library"
    HARDCOVER = "hardcover", "Hardcover"
    COMICVINE = "comicvine", "Comic Vine"
    BGG = "bgg", "BoardGameGeek"
    MUSICBRAINZ = "musicbrainz", "MusicBrainz"
    POCKETCASTS = "pocketcasts", "Pocket Casts"
    AUDIOBOOKSHELF = "audiobookshelf", "Audiobookshelf"
    MANUAL = "manual", "Manual"
```

## Summary Table

| Concept | Field/Variable | Controls | Mutable? |
|---------|---------------|----------|----------|
| **Identity Provider** | `Item.source` | Original tracking source; progress/history tiepoint; URL route source; episode/season ordering | No (requires migration) |
| **Display Provider** | User pref / `MetadataProviderPreference` | Which provider's metadata to show (images, titles, synopsis) | Yes (per-item or global) |
| **Route Source** | URL `source` parameter | Used to fetch initial metadata; typically equals identity provider | Depends on URL |

## Practical Examples

### Example 1: Same Provider
- Add "Attack on Titan" via MAL
- `Item.source = "mal"`, `Item.media_id = "16498"`
- No display override set
- Result: Shows MAL images, titles, episode order

### Example 2: Different Display Provider
- Same "Attack on Titan" (tracked on MAL)
- Set display provider to TMDB in preferences
- Result: 
  - Images/synopsis from TMDB (via cross-ID lookup)
  - Episode order still from MAL (identity provider)
  - URL remains `/anime/16498/.../mal`

### Example 3: Grouped Anime
- Add "One Piece" flat entry via MAL
- System auto-groups into TV-style seasons via TMDB mapping
- Result:
  - `Item.source = "mal"`, `Item.library_media_type = "anime"`
  - Seasons/episodes mapped from TMDB structure
  - Display provider can still override visuals

## Related Documentation

- [`docs/agents/media_type_integration.md`](media_type_integration.md): Adding new media types
- [`docs/agents/metadata-backfill.md`](metadata-backfill.md): Metadata population system
- [`src/app/services/metadata_resolution.py`](src/app/services/metadata_resolution.py): Core resolution logic
- [`src/app/models.py`](src/app/models.py): Data models (`Item`, `MetadataProviderPreference`, `ItemProviderLink`)
