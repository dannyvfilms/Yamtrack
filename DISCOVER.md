# Discover — Tab / Endpoint Reference

Overview of the editorial **tabs** planned for the Discover page, per media type, and
the provider endpoint that backs each one. This is the source-of-truth mapping the
implementation plan builds against.

The first tab in each table is **Trending** and is selected by default.

## Legend

| Symbol | Status meaning |
|---|---|
| ✅ wired | Row builder already exists and is surfaced today (`discover/provider_candidates.py`). |
| ⚠️ adapter-ready | Adapter method exists (`discover/providers/*.py`) but is **not yet surfaced** as a row/tab. |
| ⚠️ param | Existing builder already supports this via a parameter (e.g. a different `period`/`ranking_type`). |
| ❌ new | Needs a new provider method **and** a new candidate builder. |

**Key** = the Django setting (`config/settings.py`) the backing adapter reads. A tab is
greyed-out with a tooltip when its key is missing/placeholder. Note that TMDb, MAL, BGG,
ComicVine, IGDB, Trakt and Simkl ship with **default test keys**, so "present" detection
must distinguish a real key from the bundled placeholder (see plan, Phase 3).

---

## Movies

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | Trakt `GET /movies/watched/weekly` | ✅ wired (`movie_watched_weekly`) | `TRAKT_API` |
| Trending Now | TMDb `GET /trending/movie/day` | ⚠️ adapter-ready (`trending`) | `TMDB_API` |
| Top Rated | TMDb `GET /movie/top_rated` | ⚠️ adapter-ready (`top_rated`) | `TMDB_API` |
| Popular | Trakt `GET /movies/popular` | ✅ wired (`movie_popular`) | `TRAKT_API` |
| Now Playing | TMDb `GET /movie/now_playing` | ⚠️ adapter-ready (`current_cycle`) | `TMDB_API` |
| Upcoming | Trakt `GET /movies/anticipated` | ✅ wired (`movie_anticipated`) | `TRAKT_API` |
| Box Office | Trakt `GET /movies/boxoffice` | ❌ new | `TRAKT_API` |

## TV Shows

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | Trakt `GET /shows/watched/weekly` | ✅ wired (`show_watched_weekly`) | `TRAKT_API` |
| Trending Now | TMDb `GET /trending/tv/day` | ⚠️ adapter-ready (`trending`) | `TMDB_API` |
| Top Rated | TMDb `GET /tv/top_rated` | ⚠️ adapter-ready (`top_rated`) | `TMDB_API` |
| Popular | Trakt `GET /shows/popular` | ✅ wired (`show_popular`) | `TRAKT_API` |
| On The Air | TMDb `GET /tv/on_the_air` | ⚠️ adapter-ready (`current_cycle`) | `TMDB_API` |
| Airing Today | TMDb `GET /tv/airing_today` | ❌ new | `TMDB_API` |
| Coming Soon | Trakt `GET /shows/anticipated` | ✅ wired (`show_anticipated`) | `TRAKT_API` |

## Anime

Anime "Trending" stays on Trakt (genre-filtered); seasonal/ranking browsing comes from
MAL (`app/providers/mal.py`, `base_url = https://api.myanimelist.net/v2`).

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | Trakt `GET /shows/watched/weekly?genres=anime` | ✅ wired (`show_watched_weekly`, anime path) | `TRAKT_API` |
| This Season | MAL `GET /anime/season/{year}/{season}?sort=anime_num_list_users` | ❌ new | `MAL_API` |
| Last Season | MAL `GET /anime/season/{prevYear}/{prevSeason}` | ❌ new | `MAL_API` |
| Top Rated | MAL `GET /anime/ranking?ranking_type=all` | ❌ new | `MAL_API` |
| Top Airing | MAL `GET /anime/ranking?ranking_type=airing` | ❌ new | `MAL_API` |
| Most Popular | MAL `GET /anime/ranking?ranking_type=bypopularity` | ❌ new | `MAL_API` |
| Coming Soon | MAL `GET /anime/ranking?ranking_type=upcoming` | ❌ new | `MAL_API` |

`season` ∈ `winter, spring, summer, fall`; year+season are computed from today's date and
"Last Season" is the previous quarter. Extra `ranking_type` values available if more tabs
are wanted later: `favorite, tv, movie, ova, special`. List calls need an explicit
`fields=` param (e.g. `id,title,main_picture,mean,num_list_users,start_season,genres,media_type`)
to populate the cards — same as `mal.anime()` today.

## Manga

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending / Top | MAL `GET /manga/ranking?ranking_type=all\|bypopularity\|favorite` | ✅ wired (`_mal_manga_ranking_candidates`) | `MAL_API` |
| Publishing Now | MAL `GET /manga/ranking?ranking_type=manga` (+ status filter) | ❌ new | `MAL_API` |

## Books

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | OpenLibrary `GET /trending/daily.json` | ✅ wired (`_openlibrary_trending_candidates`) | none |
| This Week | OpenLibrary `GET /trending/weekly.json` | ⚠️ param (`period`) | none |
| This Month | OpenLibrary `GET /trending/monthly.json` | ⚠️ param (`period`) | none |
| This Year | OpenLibrary `GET /trending/yearly.json` | ⚠️ param (`period`) | none |
| Coming Soon | OpenLibrary upcoming editions | ✅ wired (`_openlibrary_coming_soon_candidates`) | none |

> Optional richer source: Hardcover GraphQL trending/top (`HARDCOVER_API`) — `❌ new`.

## Comics

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | ComicVine volumes | ✅ wired (`_comicvine_volume_candidates`) | `COMICVINE_API` |
| Coming Soon | ComicVine coming-soon volumes | ✅ wired (`_comicvine_coming_soon_volume_candidates`) | `COMICVINE_API` |

## Board Games

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Hot / Trending | BGG `GET /xmlapi2/hot` | ✅ wired (`_bgg_hot_candidates`) | `BGG_API_TOKEN` |

> BGG has no public "top ranked" list API; a ranked tab would require scraping `/browse`.

## Music

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | Last.fm `chart.gettoptracks` | ✅ wired (`_lastfm_top_tracks_candidates`) | `LASTFM_API_KEY` |
| Coming Soon | MusicBrainz upcoming recordings | ✅ wired (`_musicbrainz_coming_soon_recording_candidates`) | none |
| Top Artists | Last.fm `chart.gettopartists` | ❌ new | `LASTFM_API_KEY` |

> `LASTFM_API_KEY` defaults to empty — the Last.fm tabs are the clearest grey-out case.

## Podcasts

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Top / Trending | iTunes RSS `toppodcasts` | ✅ wired (`_itunes_top_podcasts_candidates`) | none |
| Genre Charts | iTunes RSS genre-scoped feeds | ❌ new | none |

## Games

| Tab | Source · endpoint | Status | Key |
|---|---|---|---|
| Trending | IGDB `/games` `sort total_rating_count desc` (recent) | ✅ wired (`_igdb_games_candidates`) | `IGDB_ID` + `IGDB_SECRET` |
| Top Rated | IGDB `/games` `sort total_rating desc` | ❌ new | `IGDB_ID` + `IGDB_SECRET` |
| Coming Soon | IGDB `/games` `sort hypes desc` + future `first_release_date` | ❌ new | `IGDB_ID` + `IGDB_SECRET` |

---

## Provider → key → adapter quick reference

| Provider | Setting(s) | Discover code |
|---|---|---|
| TMDb | `TMDB_API` | `discover/providers/tmdb_adapter.py` |
| Trakt | `TRAKT_API` (+ `TRAKT_API_SECRET`) | `discover/providers/trakt_adapter.py` |
| MAL | `MAL_API` | `app/providers/mal.py` + `_mal_*_candidates` |
| IGDB | `IGDB_ID`, `IGDB_SECRET` | `_igdb_games_candidates` |
| OpenLibrary | none | `_openlibrary_*_candidates` |
| ComicVine | `COMICVINE_API` | `_comicvine_*_candidates` |
| BGG | `BGG_API_TOKEN` | `_bgg_hot_candidates` |
| MusicBrainz | none | `_musicbrainz_*_candidates` |
| Last.fm | `LASTFM_API_KEY` | `_lastfm_top_tracks_candidates` |
| iTunes | none | `_itunes_top_podcasts_candidates` |
</content>
