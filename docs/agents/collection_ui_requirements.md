# Collection Feature UI Requirements & Testing Checklist

This document outlines the required UI components and testing points for the Collection feature. All backend tests must pass before implementing these UI components.

## UI Components Required

### 1. Collection Entry Modal/Form

**Location**: Accessible from media detail pages and collection list page

**Fields**:
- Item (hidden, auto-populated)
- Media Type (text input): e.g., "bluray", "dvd", "digital", "steam", "hardcover", "paperback"
- Resolution (text input): e.g., "1080p", "4k" (for video media)
- HDR (text input): e.g., "HDR10", "Dolby Vision" (for video media)
- 3D (checkbox): Boolean flag
- Audio Codec (text input): e.g., "DTS", "TrueHD", "Atmos", "AAC" (for video/audio media)
- Audio Channels (text input): e.g., "2.0", "5.1", "7.1.2" (for video/audio media)

**Behavior**:
- Pre-populate fields if collection entry already exists
- Show empty form for new entries
- Validate required fields (item must exist)
- Submit via AJAX (HTMX) or form POST
- Display success/error messages
- Close modal on successful submission

**Testing Points**:
- [ ] Modal opens from "Add to Collection" button
- [ ] Modal opens from "Edit Collection" button (if entry exists)
- [ ] Form fields pre-populate correctly for existing entries
- [ ] Form validation works (required fields, invalid data)
- [ ] Submit creates new collection entry
- [ ] Submit updates existing collection entry
- [ ] Success message displays after submit
- [ ] Error messages display for validation failures
- [ ] Modal closes after successful submit
- [ ] Modal works for all media types (Movie, TV, Game, Book, etc.)

### 2. "Add to Collection" / "Edit Collection" Button

**Location**: Media detail pages (`src/templates/app/media_details.html`)

**Behavior**:
- Show "Add to Collection" if item not in collection
- Show "Edit Collection" if item already in collection
- Button opens collection modal
- Button should be visible for all media types (except Podcasts)

**Testing Points**:
- [ ] Button appears on media detail pages for all supported media types
- [ ] Button text changes based on collection status
- [ ] Button opens collection modal on click
- [ ] Button hidden/not shown for Podcasts
- [ ] Button works for Movies, TV, Anime, Manga, Games, Books, Comics, Music

### 3. Collection Status Indicator

**Location**: Media cards (home page, media list pages)

**Visual Indicator**:
- Badge/icon showing item is in collection
- Optional: Tooltip showing collection metadata summary (format, resolution, etc.)

**Testing Points**:
- [ ] Collection indicator appears on media cards for collected items
- [ ] Indicator does not appear for non-collected items
- [ ] Indicator appears on home page media cards
- [ ] Indicator appears on media list page cards
- [ ] Indicator works for all media types
- [ ] Tooltip (if implemented) shows correct metadata

### 4. Collection List Page

**Location**: `/collection/` and `/collection/<media_type>/`

**Features**:
- Display all user's collection entries
- Filter by media_type (optional URL parameter)
- Sort by collected_at (newest first, default)
- Display collection metadata (format, resolution, etc.) in cards/list
- "Edit" button for each entry
- "Remove" button for each entry
- Search/filter functionality (optional)

**Testing Points**:
- [ ] Page loads for authenticated users
- [ ] Page redirects unauthenticated users to login
- [ ] All collection entries display correctly
- [ ] Filter by media_type works (URL parameter)
- [ ] Sorting by collected_at works (newest first)
- [ ] Collection metadata displays in cards/list
- [ ] "Edit" button opens modal with pre-populated data
- [ ] "Remove" button deletes collection entry
- [ ] Empty collection shows appropriate message
- [ ] Pagination works (if implemented)
- [ ] Search/filter works (if implemented)

### 5. Collection Filter on Media List Pages

**Location**: Media list pages (`/medialist/<media_type>`)

**Feature**:
- Filter toggle/checkbox: "Show only collected items"
- Filter persisted in URL or session

**Testing Points**:
- [ ] Filter toggle appears on media list pages
- [ ] Filter shows only collected items when enabled
- [ ] Filter shows all items when disabled
- [ ] Filter state persists (URL parameter or session)
- [ ] Filter works for all media types

### 6. Collection Metadata Display

**Location**: Media detail pages, collection list page

**Display Format**:
- Show collection metadata in a dedicated section or badge
- Format: "Blu-ray • 4K • HDR10 • DTS 5.1" (example for movies)
- Format: "Steam • PC" (example for games)
- Format: "Hardcover" (example for books)
- Hide fields that are blank/not applicable

**Testing Points**:
- [ ] Collection metadata displays on media detail pages
- [ ] Metadata formatting is readable and concise
- [ ] Blank/empty fields are hidden
- [ ] Metadata displays correctly for all media types
- [ ] Metadata updates when collection entry is edited
- [ ] Metadata disappears when collection entry is removed

## UI Testing Checklist (After Backend Tests Pass)

### Functional Testing

1. **Collection Entry Creation**:
   - [ ] Can create collection entry for Movie
   - [ ] Can create collection entry for TV Show
   - [ ] Can create collection entry for Anime
   - [ ] Can create collection entry for Manga
   - [ ] Can create collection entry for Game
   - [ ] Can create collection entry for Book
   - [ ] Can create collection entry for Comic
   - [ ] Can create collection entry for Music
   - [ ] Cannot create collection entry for Podcast (feature not applicable)

2. **Collection Entry Editing**:
   - [ ] Can edit existing collection entry
   - [ ] All metadata fields can be updated
   - [ ] Changes persist after save
   - [ ] Can clear optional fields (set to blank)

3. **Collection Entry Deletion**:
   - [ ] Can remove collection entry
   - [ ] Entry is removed from collection list
   - [ ] Collection indicator disappears from media cards
   - [ ] Can re-add item to collection after removal

4. **Collection List**:
   - [ ] Collection list displays all entries
   - [ ] Filter by media_type works
   - [ ] Collection entries sort by collected_at (newest first)
   - [ ] Empty collection shows appropriate message

5. **Collection Status Indicators**:
   - [ ] Indicator appears on collected items
   - [ ] Indicator updates when item added to collection
   - [ ] Indicator updates when item removed from collection
   - [ ] Indicator works on home page
   - [ ] Indicator works on media list pages

### Media Type Specific Testing

6. **Movies**:
   - [ ] Can add movie to collection with A/V metadata
   - [ ] Collection metadata displays correctly (resolution, HDR, audio, etc.)
   - [ ] Collection entry independent of Media tracking

7. **TV Shows**:
   - [ ] Can add TV show to collection
   - [ ] Collection entry at show level (not season/episode level)
   - [ ] Collection metadata displays correctly

8. **Games**:
   - [ ] Can add game to collection with platform/store info
   - [ ] Steam imports auto-populate collection metadata
   - [ ] Manual entry works for non-Steam games
   - [ ] Platform information displays correctly

9. **Books**:
   - [ ] Can add book to collection with format (hardcover/paperback/ebook)
   - [ ] Goodreads imports auto-populate format when available
   - [ ] Manual entry works for books without format info

10. **Music**:
    - [ ] Can add music track to collection with format metadata
    - [ ] Plex Music imports auto-populate format when available
    - [ ] Collection metadata displays correctly (codec, bitrate, etc.)

11. **Comics/Manga**:
    - [ ] Can add comic/manga to collection (manual entry only)
    - [ ] Collection metadata displays correctly

### Integration Testing

12. **Import Integration**:
    - [ ] Steam import creates collection entries with platform/store info
    - [ ] Goodreads import creates collection entries with format when available
    - [ ] Collection update mode works for Plex (updates collection without importing media)
    - [ ] Post-import collection updates work (after regular imports)

13. **Webhook Integration**:
    - [ ] Plex webhook creates/updates collection entries (async)
    - [ ] Collection metadata extracted from Plex API correctly
    - [ ] Webhook doesn't block response (async task queued)

### Edge Cases

14. **Edge Cases**:
    - [ ] Cannot create duplicate collection entries (uniqueness constraint)
    - [ ] Collection entry persists when Media tracking is deleted
    - [ ] Collection entry deleted when Item is deleted (CASCADE)
    - [ ] Collection works independently of Media tracking (can have collection without tracking, and vice versa)
    - [ ] Empty/blank metadata fields handled gracefully in UI
    - [ ] Long metadata values don't break UI layout

### Responsive Design Testing

15. **Mobile/Tablet**:
    - [ ] Collection modal works on mobile devices
    - [ ] Collection list displays correctly on mobile
    - [ ] Collection indicators visible on mobile cards
    - [ ] Touch interactions work (tap to open modal, etc.)

### Accessibility Testing

16. **Accessibility**:
    - [ ] Collection buttons/keyboard accessible
    - [ ] Collection forms keyboard navigable
    - [ ] Screen reader announces collection status
    - [ ] Color contrast meets WCAG standards
    - [ ] Focus indicators visible

## Implementation Order

1. **Backend Implementation** (current plan)
   - Complete all backend code
   - Write and verify all backend tests pass
   - Verify all media types work
   - Verify all data sources work

2. **UI Implementation** (after backend verified)
   - Collection modal/form component
   - "Add to Collection" button on detail pages
   - Collection list page
   - Collection status indicators
   - Collection filters

3. **UI Testing** (after UI implementation)
   - Run through all testing checklist items
   - Verify all media types work in UI
   - Verify all integrations work in UI
   - Fix any UI bugs found

## Backend Testing Status

✅ **Backend Testing Completed** (2026-01-23)
- All backend models, views, forms, and helpers implemented and tested
- Plex webhook integration for music tracks verified working
- Collection metadata extraction from Plex API confirmed working
- Database queries and aggregation functions tested
- Collection entry creation/update/delete operations verified

**Test Results:**
- Music track collection metadata successfully extracted and stored (audio_codec: MP3, audio_channels: 2.0)
- Plex webhook integration queues collection updates correctly
- Album-level collection metadata aggregation implemented and ready for UI display

## Notes

- All UI components should use existing design patterns from the codebase (Tailwind CSS, HTMX patterns, etc.)
- Collection metadata should be optional - users can add items to collection without metadata
- Collection status should be clearly visible but not intrusive
- Consider adding collection statistics to statistics page (future enhancement)
- **Album Page Collection Display**: Collection metadata chips are displayed at the album level (aggregated from all tracks) in the badge section alongside "Album + Soundtrack 2024" chips
