# Yamtrack

A self-hosted Trakt replacement built on Yamtrack, with unified History, Time Left / Progress, richer stats, smarter lists, deeper integrations, and the daily-driver polish former Trakt users usually miss.

[Demo](https://yamtrack.dannyvfilms.com) | [Docker Image](https://github.com/dannyvfilms/Yamtrack/pkgs/container/yamtrack) | [Releases](https://github.com/dannyvfilms/Yamtrack/releases)

Credentials for Demo:
- Username: `demo`
- Password: `demodemo`

## What this fork adds

The fork is built around the workflows Trakt used to cover well: Time Left progress, a unified history feed, recap-style stats, public list sharing, and all-in-one tracking. The sections below show what has been added to get there.

### Major additions

- **Music**: artist and album pages, track-level history and scoring, play-count and listening-time statistics, bulk save and mark-all-listened; MusicBrainz-backed metadata with discography sync and cover art; fully native in history, search, home rows, and collection, not a thin importer.
- **Podcasts**: dedicated show and episode pages, episode-level tracking, mark-all-played; Pocket Casts account sync as a live integration, not a one-shot file import; podcast listening appears naturally in history, runtime stats, and search.
- **Collections / owned media**: track what you physically or digitally own with copy-level detail: source, resolution, HDR, format, codec, and bitrate; filtered collection views, per-item collection status tied into detail pages and list/smart-list rules; supports Plex collection sync.
- **Discover**: personalized recommendation rows that improve with use: genre, studio, cast, and tag affinity built from your library; not-interested and hide feedback that sticks; background refresh so rows stay current, individually refreshable from the UI, not a static recommendations page.
- **History and statistics**: history rebuilt as a filterable feed with month navigation, media-type and genre filters, inline duplicate-play cleanup, and a delete flow; statistics rebuilt with explicit refresh, compare mode, custom date ranges, top-talent breakdowns, and per-type splits covering TV, film, music, podcasts, and reading with pages read, top authors, reading streaks, and listening time.
- **Lists: public, social, and smart**: public and private lists, custom slugs, public profile pages; RSS and JSON feeds per list; smart-list rules for collection status, release state, platform, origin, author, and tags; recommendations with approval flow; list completion percentages and media-type breakdowns in the index; Trakt list and watchlist import; sort by rating, progress, release date, last watched, or custom manual order.
- **Integration coverage**: Plex full library import, watchlist sync, and ratings sync; Pocket Casts account sync; Last.fm history import and live poll; Audiobookshelf account import; Radarr and Sonarr scheduled library sync; Jellyseerr webhook auto-add, each with dedicated settings and status display.

### Minor additions

- **Richer metadata and title control**: localized and original titles switchable per user preference; critic ratings and popularity scores displayed; game-length data; manual metadata overrides; metadata-provider preference; image refresh flows.
- **People, studios, and credit browsing**: actor, director, author, and studio pages with filmographies and top works; person credits visible from detail pages rather than hidden as tooltip data; author pages with top-read breakdowns.
- **Better anime handling**: proper separation of anime and TV library concerns so mixed libraries stay organized; anime-specific season and episode navigation; grouped-anime routing for franchise-spanning series.
- **Richer episode and book workflows**: episode detail pages with individual scoring; bulk episode save; drop an episode without logging it to history; book-specific: barcode and ISBN scanning from a photo, percentage-based reading progress, top-authors stats, and more resilient import flows.
- **Configurable home screen**: choose what rows appear and in what order; rows from library queries, custom lists, smart lists, or recently played but not rated; direction and media-type filters stored per user.
- **Configurable table columns**: choose and reorder visible columns per view, with media tables and list-detail tables configured independently; available columns include critic rating, episodes left, time left, time to beat, runtime, time watched, last watched, next air date, date added, popularity, and more.
- **Scheduled backups and richer export management**: recurring export scheduling with media-type and list inclusion options; export history and backup destination visible in settings.
- **Account security**: TOTP authenticator setup and management; recovery codes; password recovery via authenticator or recovery code; session duration as a per-user preference.

### Quality-of-life changes

- **Much deeper preferences**: sort modes for critic rating, popularity, runtime, time to beat, plays, time watched, release date, last watched, next air date, and time left; display preferences for duration format, rating scale, stats default range, compare mode, mobile grid density, subtitle visibility on cards, localized vs. original title display, progress-bar visibility, planned-item home visibility, and obfuscating unseen episode titles.
- **Livelier UI**: a now-playing card showing what is actively playing via Plex, Jellyfin, or Emby webhook; explicit stale and refreshing indicators on history and stats with one-click refresh; lazy-loaded covers and asynchronous fragments throughout.
- **Better search and add flows**: music-native search that creates artist and album entries from search results; improved anime and localized-title search results.
- **Deeper filters**: rated and unrated, collected and not collected, caught-up and not-caught-up, no-status, language, country, platform, origin, format, author, tag inclusion, and tag exclusion; smart-list rules use the same expanded vocabulary, making them meaningfully programmable.
- **More reliable under load**: WAL mode and timeout configuration for SQLite; retry logic for lock and I/O failures; prioritized background task queues for a smoother experience with large libraries.
- **Integration settings and import UX**: import history and status visible per integration in settings; watchlist-only and collection-update-only import modes; Jellyseerr allowed usernames and defaults persisted as preferences; per-user Plex webhook library selection.

## Screenshots

### Time Left / Progress

The Progress view is built to make active shows, backlog triage, and planning visible at a glance.

<img alt="Time Left progress view" src="https://github.com/user-attachments/assets/ab5594ea-6ddc-4512-9837-87b68ec874c2" />

### History

History keeps watches and listens in one place so recent activity is easy to scan.

<img alt="History page" src="https://github.com/user-attachments/assets/18927954-dd57-40ba-86ef-11ae986cf9ee" />

### Statistics

Statistics are designed for recap-style browsing across time ranges and media types.

<img alt="Statistics dashboard" src="https://github.com/user-attachments/assets/be9da320-d745-45aa-8c6a-23efa66d0c6c" />

### Shareable Lists

Lists can be shared publicly, surfaced on profiles, and used as more than a private backlog.

<img alt="Shareable lists" src="https://github.com/user-attachments/assets/ef13eff1-dc14-4ab6-b5d0-39598a1264ec" />

### Collections / Owned Media

Collections add ownership context alongside tracking, with room for copy-level detail.

<table>
  <tr>
    <td valign="top">
      <img width="1296" height="643" alt="Screenshot 2026-04-10 at 7 47 58 PM" src="https://github.com/user-attachments/assets/28bdac5a-1678-4144-a227-0d361912882c" />
    </td>
    <td valign="top">
      <img width="508" height="631" alt="Screenshot 2026-04-10 at 7 48 52 PM" src="https://github.com/user-attachments/assets/a2c8deb9-2d92-4aaa-b605-758871f36634" />
    </td>
  </tr>
</table>

### Music and Podcasts

Music and podcasts are treated as first-class parts of the same tracker, not side imports.

<img alt="Music and podcasts" src="https://github.com/user-attachments/assets/0c6da813-d73e-4f7c-9d2b-ba42d65221a7" />

## What you still keep from upstream Yamtrack

This fork builds on Yamtrack's foundation instead of replacing it. You still keep:

- Tracking for movies, TV, anime, manga, games, books, comics, and board games, plus manual entries for hard-to-find media
- Multi-user accounts, OIDC and social login support, and per-user tracking data
- Calendar and iCalendar feeds for upcoming releases
- Release notifications through Apprise
- Jellyfin, Plex, and Emby playback integrations
- Import/export flows for Trakt, Simkl, MyAnimeList, AniList, Kitsu, Yamtrack CSV, and more
- Docker deployment with SQLite or PostgreSQL
- CSV export/import and self-hosted control over your data

## Quick Start

If you already know you want the fork, start here.

### Docker Compose (Recommended)

The easiest way to get started is with Docker Compose. This works well with Portainer stacks or a plain local Docker install.

Important:

- Yamtrack uses PostgreSQL only when `DB_HOST` is set.
- If `DB_HOST` is not set, Yamtrack uses SQLite at `/yamtrack/db/db.sqlite3`.
- `DATABASE_URL` is not currently supported.

**For SQLite (simple setup):**

```yaml
services:
  yamtrack:
    image: ghcr.io/dannyvfilms/yamtrack:latest
    container_name: yamtrack
    restart: unless-stopped
    depends_on:
      - redis
    environment:
      - SECRET=your-secret-key-here-change-this
      - REDIS_URL=redis://redis:6379
      - TZ=America/New_York
    volumes:
      - ./db:/yamtrack/db
    ports:
      - "8000:8000"

  redis:
    image: redis:8-alpine
    container_name: yamtrack-redis
    restart: unless-stopped
    volumes:
      - redis_data:/data

volumes:
  redis_data:
```

If you use SQLite, you must persist `/yamtrack/db`. Without that mount, recreating the container also recreates an empty database.

Save this as `docker-compose.yml` and run:

```bash
docker compose up -d
```

Then visit `http://localhost:8000` and create your first account.

**For PostgreSQL (production setup):**

```yaml
services:
  yamtrack:
    image: ghcr.io/dannyvfilms/yamtrack:latest
    container_name: yamtrack
    restart: unless-stopped
    depends_on:
      - db
      - redis
    environment:
      - SECRET=your-secret-key-here-change-this
      - REDIS_URL=redis://redis:6379
      - TZ=America/New_York
      - DB_HOST=db
      - DB_NAME=yamtrack
      - DB_USER=yamtrack
      - DB_PASSWORD=change-this-password
      - DB_PORT=5432
    ports:
      - "8000:8000"

  db:
    image: postgres:16-alpine
    container_name: yamtrack-db
    restart: unless-stopped
    environment:
      - POSTGRES_DB=yamtrack
      - POSTGRES_USER=yamtrack
      - POSTGRES_PASSWORD=change-this-password
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:8-alpine
    container_name: yamtrack-redis
    restart: unless-stopped
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

For PostgreSQL, use `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, and `DB_PORT`. Do not replace these with `DATABASE_URL`; Yamtrack falls back to SQLite if `DB_HOST` is missing.

### Docker Run

If you prefer a simple one-liner without Docker Compose:

```bash
docker network create yamtrack-net

docker run -d \
  --name yamtrack-redis \
  --network yamtrack-net \
  --restart unless-stopped \
  -v yamtrack-redis-data:/data \
  redis:8-alpine

docker run -d \
  --name yamtrack \
  --network yamtrack-net \
  --restart unless-stopped \
  -e TZ=America/New_York \
  -e SECRET=your-secret-key-here-change-this \
  -e REDIS_URL=redis://yamtrack-redis:6379 \
  -v yamtrack-db:/yamtrack/db \
  -p 8000:8000 \
  ghcr.io/dannyvfilms/yamtrack:latest
```

This setup uses named volumes (`yamtrack-db` and `yamtrack-redis-data`) and a shared network (`yamtrack-net`). For more options, use Docker Compose.

### Portainer

Portainer users should prefer **Stacks** over **Containers -> Add container**. Stacks let you paste the working compose file directly and avoid missing required volumes or env vars.

**Recommended: Portainer Stacks**

1. In Portainer, go to **Stacks** -> **Add Stack**
2. Name it `yamtrack`
3. Paste one of the compose configurations above
4. Update the `SECRET` environment variable with a secure random string
5. Deploy the stack

**If you use Containers -> Add container anyway**

- Always set `SECRET` and `REDIS_URL`.
- For SQLite, mount persistent storage to `/yamtrack/db`.
- For PostgreSQL, set `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, and `DB_PORT` on the Yamtrack container.
- For PostgreSQL, also persist `/var/lib/postgresql/data` on the Postgres container.
- Publish port `8000` from the container to a host port.
- Leave `Command` and `Entrypoint` empty unless you know you need to override them.

### Environment Variables

The only universally required variable is `SECRET` for Django's secret key. For Docker installs, you should also set `REDIS_URL` to a reachable Redis instance.

**Optional but recommended:**

- `TMDB_API` - movie and TV metadata from [TMDB](https://www.themoviedb.org/settings/api)
- `TVDB_API_KEY` / `TVDB_PIN` - TVDB-backed metadata and grouped anime support (`TVDB_PIN` is your **Subscriber PIN**, only required for user-supported API keys)
- `MAL_API` - MyAnimeList **Client ID** for anime metadata ([register here](https://myanimelist.net/apiconfig))
- `IGDB_ID` / `IGDB_SECRET` - game metadata from [IGDB](https://www.igdb.com/api)
- `STEAM_API_KEY` - Steam game imports
- `BGG_API_TOKEN` - board game metadata from [BoardGameGeek](https://boardgamegeek.com/using_the_xml_api)
- `HARDCOVER_API` - Hardcover book metadata/imports
- `COMICVINE_API` - comic metadata
- `LASTFM_API_KEY` - Last.fm integration and scrobble polling
- `TRAKT_API` / `TRAKT_API_SECRET` - Trakt private-profile OAuth imports
- `URLS` - your public URL if using a reverse proxy, for example `https://yamtrack.mydomain.com`
- `ADMIN_ENABLED` - set to `True` to enable the Django admin interface at `/admin/` (see the [Admin Guide](wiki/6.-Admin-and-Operations.md#admin-guide))

For a complete list, see the [Environment Variables documentation](wiki/6.-Admin-and-Operations.md#environment-variables).

### Persistence Checklist

- SQLite stores the app database at `/yamtrack/db/db.sqlite3`; persist `/yamtrack/db`.
- PostgreSQL stores its database files at `/var/lib/postgresql/data`; persist that path on the Postgres container.
- Redis stores sessions and background-task state; resetting Redis can log users out, but it should not delete accounts if the database is persisted.
- Do not assume `DATABASE_URL` enables PostgreSQL. Yamtrack uses Postgres only when `DB_HOST` is set.

Example `.env` file:

```bash
TMDB_API=API_KEY
TVDB_API_KEY=TVDB_API_KEY
TVDB_PIN=SUBSCRIBER_PIN (Optional, only for user-supported keys)
MAL_API=CLIENT_ID
IGDB_ID=IGDB_ID
IGDB_SECRET=IGDB_SECRET
STEAM_API_KEY=STEAM_API_SECRET
BGG_API_TOKEN=BGG_API_TOKEN
HARDCOVER_API=HARDCOVER_API
COMICVINE_API=COMICVINE_API
LASTFM_API_KEY=LASTFM_API_KEY
SECRET=SECRET
DEBUG=True
```

#### Trakt private profile import (OAuth)

If you import from a private Trakt profile, configure OAuth first:

1. Create an app in [Trakt API Apps](https://trakt.tv/oauth/applications).
2. Add this Redirect URI in the Trakt app:
   - `https://your_domain.com/import/trakt/private`
3. Set these environment variables in Yamtrack:
   - `TRAKT_API` = your Trakt client ID
   - `TRAKT_API_SECRET` = your Trakt client secret

If you run Yamtrack behind a reverse proxy, set `URLS=https://your_domain.com` so Yamtrack generates the correct external callback URL.


### Troubleshooting: I Updated and My Login Is Gone

If an update recreated the container and your account is gone:

1. If you intended to use PostgreSQL, confirm `DB_HOST` is set. `DATABASE_URL` alone will not enable Postgres.
2. If you intended to use SQLite, confirm `/yamtrack/db` is mounted to persistent storage.
3. If you were only logged out but can sign in again, Redis/session data was reset; your account database is still intact.
4. Do not remove database volumes during updates unless you intentionally want a fresh install.

### Reverse Proxy Setup

If you are using a reverse proxy (Nginx, Traefik, Caddy, and so on) and see a `403 Forbidden` error, add your URL to the environment variables:

```yaml
environment:
  - URLS=https://yamtrack.mydomain.com
```

Multiple origins can be specified with commas, for example `https://yamtrack.mydomain.com,https://yamtrack-alt.mydomain.com`.

If Yamtrack does not generate the correct callback URLs for authenticating with Anilist and other imports, add this to your environment variables:

```yaml
environment:
  - USE_X_FORWARDED=True
```

> **Note:** If you are using a Cloudflare Tunnel or any HTTPS-terminating proxy, also set `USE_X_FORWARDED_PROTO=True` — otherwise Django cannot detect the correct scheme and CSRF checks will fail.

### Docker Image Tags

The Docker image is available at `ghcr.io/dannyvfilms/yamtrack` with these tags:

- `:latest` - the latest commit on this fork's `latest` branch
- `:release` - builds published from GitHub release tags
- `:vX.Y.Z` - versioned release builds
- `:dev` - the `dev` branch, which this fork keeps aligned with upstream Yamtrack

## Local Development

If you want to contribute or customize the app locally:

1. Clone the repository:

   ```bash
   git clone https://github.com/dannyvfilms/Yamtrack.git
   cd Yamtrack
   ```

2. Start Redis:

   ```bash
   docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:8-alpine
   ```

3. Create a `.env` file:

   ```bash
   TMDB_API=your_key
   TVDB_API_KEY=your_tvdb_key
   TVDB_PIN=your_subscriber_pin (Optional)
   MAL_API=your_mal_client_id
   IGDB_ID=your_id
   IGDB_SECRET=your_secret
   STEAM_API_KEY=your_key
   BGG_API_TOKEN=your_bgg_token
   HARDCOVER_API=your_hardcover_token
   COMICVINE_API=your_comicvine_key
   LASTFM_API_KEY=your_lastfm_key
   SECRET=your_secret
   DEBUG=True
   ```

4. Install dependencies and initialize the app:

   ```bash
   python -m pip install -U -r requirements-dev.txt
   cd src
   python manage.py migrate
   python manage.py createsuperuser
   ```

5. Start services in separate terminals:

   ```bash
   python manage.py runserver
   ```

   ```bash
   celery -A config worker --queues interactive --hostname celery-interactive@%h --loglevel DEBUG

   celery -A config worker --queues celery --beat --scheduler django --hostname celery@%h --loglevel DEBUG
   ```

   ```bash
   tailwindcss -i ./static/css/input.css -o ./static/css/main.css --watch
   ```

Visit `http://localhost:8000` to use your local instance.

The fork also provisions a built-in demo account after migrations:

- Username: `demo`
- Password: `demodemo`

## Support the Project

- Star the repository if you want to help more people discover the fork.
- Open an [issue](https://github.com/dannyvfilms/Yamtrack/issues) for bugs or missing migration workflows you run into.
- Use [GitHub issues](https://github.com/dannyvfilms/Yamtrack/issues) for feature requests and fork-specific improvement ideas.
- Open a pull request if you want to contribute code, docs, or polish.

## License

This project is licensed under the AGPL-3.0 License.

## Acknowledgments

This fork is based on [FuzzyGrim/Yamtrack](https://github.com/FuzzyGrim/Yamtrack), which remains the foundation. This repository focuses on the Trakt-replacement, daily-driver direction for users who want a more opinionated and feature-dense Yamtrack experience.
