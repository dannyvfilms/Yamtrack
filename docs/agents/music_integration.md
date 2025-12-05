# Music Media Type Integration

This document provides a comprehensive reference for the Music media type implementation in Yamtrack, including the hierarchical data model, MusicBrainz provider integration, and UI patterns.

## Overview

Music follows a **TV → Season → Episode** pattern but adapted for **Artist → Album → Track**:

| TV Pattern | Music Pattern |
|------------|---------------|
| TV (show) | Artist |
| Season | Album |
| Episode | Track |
| TV (Media subclass) | ArtistTracker |
| Season (Media subclass) | AlbumTracker |
| Episode (separate model) | Track (separate model) |
| Movie (Media subclass) | Music (Media subclass) |

Key differences from TV:
- Artist and Album are **container models** (not Media subclasses)
- `ArtistTracker` and `AlbumTracker` handle user tracking for artists/albums
- `Music` (Media subclass) tracks individual songs (like how Episode tracks individual episodes)
- `Track` is the metadata catalog (like Episode), populated from MusicBrainz

## Data Model

### Core Models (`src/app/models.py`)

#### Artist
Container for music artists. Not a Media subclass.

```python
class Artist(models.Model):
    name = models.CharField(max_length=255)
    sort_name = models.CharField(max_length=255)
    musicbrainz_id = models.CharField(max_length=36, unique=True, null=True)
    image = models.URLField()  # Wikipedia photo
    discography_synced_at = models.DateTimeField(null=True)  # When albums were fetched
```

#### Album
Container for albums. Not a Media subclass.

```python
class Album(models.Model):
    title = models.CharField(max_length=255)
    musicbrainz_release_id = models.CharField(max_length=36, null=True)  # Specific release
    musicbrainz_release_group_id = models.CharField(max_length=36, null=True)  # Release group
    artist = models.ForeignKey(Artist, related_name="albums")
    release_date = models.DateField(null=True)
    image = models.URLField()  # Cover art from Cover Art Archive
    release_type = models.CharField()  # "Album", "EP", "Compilation", etc.
    tracks_populated = models.BooleanField()  # Whether Track rows exist
```

#### Track
Metadata catalog for tracks (like Episode). Not per-user.

```python
class Track(models.Model):
    album = models.ForeignKey(Album, related_name="tracklist")
    title = models.CharField(max_length=500)
    musicbrainz_recording_id = models.CharField(max_length=36, null=True)
    track_number = models.PositiveIntegerField(null=True)
    disc_number = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(null=True)
    
    @property
    def duration_formatted(self):
        """Return duration as mm:ss string."""
```

#### Music (Media subclass)
Per-user tracking for individual songs.

```python
class Music(Media):
    album = models.ForeignKey(Album, related_name="music_entries", null=True)
    artist = models.ForeignKey(Artist, related_name="music_entries", null=True)
    track = models.ForeignKey(Track, related_name="music_entries", null=True)
    
    @property
    def formatted_progress(self):
        """Return progress as play count."""
```

#### ArtistTracker
Per-user tracking for artists (like TV show tracking).

```python
class ArtistTracker(models.Model):
    user = models.ForeignKey(User, related_name="artist_trackers")
    artist = models.ForeignKey(Artist, related_name="trackers")
    status = models.CharField(choices=Status.choices)  # In Progress, Completed, etc.
    score = models.DecimalField(max_digits=3, decimal_places=1)  # 0-10
    start_date = models.DateTimeField(null=True)
    end_date = models.DateTimeField(null=True)
    notes = models.TextField()
```

#### AlbumTracker
Per-user tracking for albums (like Season tracking).

```python
class AlbumTracker(models.Model):
    user = models.ForeignKey(User, related_name="album_trackers")
    album = models.ForeignKey(Album, related_name="trackers")
    status = models.CharField(choices=Status.choices)
    score = models.DecimalField(max_digits=3, decimal_places=1)
    start_date = models.DateTimeField(null=True)
    end_date = models.DateTimeField(null=True)
    notes = models.TextField()
```

### Enums

```python
class Sources(models.TextChoices):
    MUSICBRAINZ = "musicbrainz", "MusicBrainz"

class MediaTypes(models.TextChoices):
    MUSIC = "music", "Music"
```

### User Preferences (`src/users/models.py`)

```python
# Per-user music settings
music_enabled = models.BooleanField(default=True)
music_layout = models.CharField(default="grid", choices=LAYOUT_CHOICES)
music_sort = models.CharField(default="title", choices=SORT_CHOICES)
music_status = models.CharField(default="all", choices=STATUS_CHOICES)
```

## Provider Integration (`src/app/providers/musicbrainz.py`)

### MusicBrainz API

Base URL: `https://musicbrainz.org/ws/2/`

Rate limiting: 1 request per second (enforced via `_rate_limit()`)

### Key Functions

#### Search Functions

```python
def search(query, page=1, skip_cover_art=False):
    """Search for music recordings (tracks)."""
    # Searches /recording endpoint
    # Returns: media_id, title, artist_name, album_title, image, duration_minutes

def search_artists(query, page=1):
    """Search for artists."""
    # Searches /artist endpoint
    # Returns: artist_id, name, disambiguation, type, country

def search_releases(query, page=1, skip_cover_art=False):
    """Search for releases (albums)."""
    # Searches /release endpoint
    # Returns: release_id, title, artist_name, artist_id, release_date, image

def search_combined(query, page=1):
    """Combined search for music (artists + albums + tracks)."""
    # Page 1: Top 5 artists, top 5 albums, 20 tracks
    # Page 2+: Just tracks (pagination)
    # skip_cover_art=True for faster search
```

#### Metadata Functions

```python
def get_artist(artist_id):
    """Get detailed artist metadata."""
    # Fetches from /artist/{id} with inc=url-rels+genres+tags+ratings
    # Gets Wikipedia bio and image via URL relations
    # Returns: name, type, country, genres, tags, rating, bio, image

def get_artist_discography(artist_id, skip_cover_art=False):
    """Get all albums for an artist."""
    # Fetches /release-group with artist filter
    # Filters to Album, EP, Compilation types
    # Returns list of albums with cover art (unless skip_cover_art=True)

def recording(media_id):
    """Get detailed track metadata."""
    # Fetches /recording/{id} with inc=releases+artists
    # Returns: title, artist, album, duration, release_date, image
```

#### Cover Art Functions

```python
def get_cover_art(release_id=None, release_group_id=None):
    """Get cover art from Cover Art Archive."""
    # Tries release_id first, then release_group_id as fallback
    # Prefers "Front" cover type
    # Returns image URL or settings.IMG_NONE

def _get_cover_art(release_id):
    """Internal cover art fetch (single release)."""
    # Fetches from https://coverartarchive.org/release/{id}
    # Handles redirects to archive.org
```

#### Wikipedia Integration

```python
def get_wikipedia_data(title):
    """Fetch Wikipedia bio and image."""
    # Uses Wikipedia REST API: /api/rest_v1/page/summary/{title}
    # Returns: extract (bio text), image (photo URL)
```

### Wikipedia Bio Strategy

1. Check MusicBrainz `url-rels` for Wikipedia link (e.g., `Queen_(band)`)
2. If found, use exact Wikipedia article title
3. If not, try artist name directly (works for `Kenny G`)
4. Fall back to `{name}_{disambiguation}` (e.g., `Queen_(band)`)

## Services (`src/app/services/music.py`)

### Discography Sync

```python
def needs_discography_sync(artist: Artist) -> bool:
    """Check if discography needs syncing."""
    # True if never synced or synced > 7 days ago

def sync_artist_discography(artist: Artist, force: bool = False) -> int:
    """Sync all albums from MusicBrainz."""
    # Creates/updates Album records
    # Skips cover art for speed (loaded async via HTMX)
    # Sets artist.discography_synced_at
```

### Track Population

```python
def populate_album_tracks(album: Album) -> int:
    """Populate Track rows for an album."""
    # Called when viewing album detail
    # Fetches tracks from MusicBrainz
    # Creates Track rows with title, number, duration
```

### Cover Art Prefetch

```python
def prefetch_album_covers(artist: Artist, limit: int = 20):
    """Prefetch cover art for albums missing images."""
    # Called async via HTMX after artist page loads
    # Only fetches for albums with missing images
    # Respects rate limits
```

## Views (`src/app/views.py`)

### Artist Views

```python
@require_GET
def artist_detail(request, artist_id):
    """Artist detail page (like TV show detail)."""
    # Syncs discography if needed
    # Gets Wikipedia bio/image
    # Calculates play counts per album
    # Shows ArtistTracker modal for tracking

@require_GET
def prefetch_artist_covers(request, artist_id):
    """HTMX endpoint for async cover art loading."""
    # Called 500ms after artist page loads
    # Fetches missing album covers
    # Returns updated album grid HTML
```

### Album Views

```python
@require_GET
def album_detail(request, album_id):
    """Album detail page (like Season detail)."""
    # Populates tracks if needed
    # Shows track list with play status
    # AlbumTracker modal for album tracking
```

### Tracking Views

```python
@require_GET
def artist_track_modal(request, artist_id):
    """Render artist tracking modal."""
    # Same UI pattern as TV show tracking

@require_POST
def artist_save(request):
    """Save/update ArtistTracker."""

@require_POST
def artist_delete(request):
    """Delete ArtistTracker."""

@require_GET
def album_track_modal(request, album_id):
    """Render album tracking modal."""

@require_POST
def album_save(request):
    """Save/update AlbumTracker."""

@require_POST
def album_delete(request):
    """Delete AlbumTracker."""

@require_POST
def song_save(request):
    """Add a listen for a track (like episode_save)."""
```

### Creation Views

```python
@require_GET
def create_artist_from_search(request, artist_mbid):
    """Create Artist from MusicBrainz ID."""
    # Fetches artist metadata
    # Gets Wikipedia bio/image
    # Syncs discography (skip_cover_art=True)
    # Redirects to artist detail

@require_GET
def create_album_from_search(request, release_mbid):
    """Create Album from MusicBrainz release ID."""
```

## URL Patterns (`src/app/urls.py`)

```python
# Artist navigation
path("music/artist/<int:artist_id>/", views.artist_detail, name="artist_detail"),
path("music/artist/<int:artist_id>/covers/", views.prefetch_artist_covers, name="prefetch_artist_covers"),

# Album navigation
path("music/album/<int:album_id>/", views.album_detail, name="album_detail"),

# Artist tracking
path("music/artist/<int:artist_id>/track_modal/", views.artist_track_modal, name="artist_track_modal"),
path("music/artist/save/", views.artist_save, name="artist_save"),
path("music/artist/delete/", views.artist_delete, name="artist_delete"),

# Album tracking
path("music/album/<int:album_id>/track_modal/", views.album_track_modal, name="album_track_modal"),
path("music/album/save/", views.album_save, name="album_save"),
path("music/album/delete/", views.album_delete, name="album_delete"),

# Track/song tracking
path("music/song/save/", views.song_save, name="song_save"),

# Creation from search
path("music/artist/create/<str:artist_mbid>/", views.create_artist_from_search, name="create_artist_from_search"),
path("music/album/create/<str:release_mbid>/", views.create_album_from_search, name="create_album_from_search"),

# Metadata sync
path("music/artist/<int:artist_id>/sync/", views.sync_artist_discography_view, name="sync_artist_discography"),
path("music/album/<int:album_id>/sync/", views.sync_album_metadata_view, name="sync_album_metadata"),
```

## Templates

### Artist Detail (`src/templates/app/music_artist_detail.html`)

Layout matches TV show detail:
- **Hero section**: Artist image (from Wikipedia), name, genre chips, bio
- **Score cards**: MB Score (from MusicBrainz rating), Your Score (from ArtistTracker)
- **Action button**: "Add to Library" / status button (opens tracking modal)
- **Left column**: Your History, Actions (lists, history, sync), Details (type, origin, dates)
- **Right column**: Album grid with async cover loading via HTMX

### Album Detail (`src/templates/app/music_album_detail.html`)

Layout matches Season detail:
- **Hero section**: Album cover, title, artist link, release type/year chips, stats
- **Action button**: Album tracking modal
- **Left column**: Your History, Actions, Details
- **Right column**: Track list with "Track Song" buttons for each track

### Album Grid (`src/templates/app/components/album_grid.html`)

Reusable component for displaying album cards:
- Cover art (or placeholder icon)
- Album title
- Release year and type
- Play count badge (if user has listens)

### Tracking Modals

- `artist_track_modal.html`: Same pattern as `fill_track.html` for TV
- `album_track_modal.html`: Same pattern for albums
- `fill_track_song.html`: Same pattern as `fill_track_episode.html` for tracks

### Music List (`src/templates/app/media_list.html`)

When `is_artist_list=True`:
- Shows `ArtistTracker` entries (not `Music` entries)
- Uses `artist_grid_items.html` or `artist_table_items.html`
- Mirrors TV show list behavior

## Forms (`src/app/forms.py`)

```python
class MusicForm(MediaForm):
    """Form for Music tracking."""
    
class ArtistTrackerForm(forms.ModelForm):
    """Form for artist tracking modal."""
    class Meta:
        model = ArtistTracker
        fields = ["status", "score", "start_date", "end_date", "notes"]

class AlbumTrackerForm(forms.ModelForm):
    """Form for album tracking modal."""
    class Meta:
        model = AlbumTracker
        fields = ["status", "score", "start_date", "end_date", "notes"]
```

## Admin (`src/app/admin.py`)

Custom admin classes for music models:
- `ArtistAdmin`: Searchable by name, filterable by discography sync status
- `AlbumAdmin`: Searchable by title, filterable by artist and release type
- `TrackAdmin`: Inline editing, filterable by album
- `ArtistTrackerAdmin`: User/artist/status filters
- `AlbumTrackerAdmin`: User/album/status filters

## Performance Optimizations

### Search Speed
- `search_combined()` passes `skip_cover_art=True` to avoid fetching cover art for every result
- Search results show placeholder images; covers load when viewing artist/album pages

### Artist Page Load
- Discography sync skips cover art fetching (`skip_cover_art=True`)
- Cover art loads asynchronously via HTMX endpoint (`prefetch_artist_covers`)
- Page renders immediately with placeholder images; covers fill in progressively

### Caching
- MusicBrainz API responses cached for 24 hours to 7 days
- Wikipedia data cached for 7 days
- Cover art URLs cached for 7 days
- Failed lookups cached for 1 day to avoid repeated failures

## What's NOT Implemented (Future Phases)

- Plex/Jellyfin webhook integration for scrobbling
- Statistics charts for music
- Calendar events for album releases
- Import/export for music library
- Auto-pause for music playback

## Checklist for Music-Related Changes

1. **Adding new music metadata**: Update `musicbrainz.py` provider functions
2. **Changing artist display**: Update `music_artist_detail.html` and `artist_grid_items.html`
3. **Changing album display**: Update `music_album_detail.html` and `album_grid.html`
4. **Adding tracking features**: Mirror TV show/season patterns in views and templates
5. **Performance issues**: Check if cover art is being fetched synchronously; use `skip_cover_art=True`
6. **Missing Wikipedia data**: Check URL relations strategy in `get_artist()`

