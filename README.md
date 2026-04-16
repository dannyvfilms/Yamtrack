# Yamtrack

A self-hosted Trakt replacement built on Yamtrack, with unified History, Time Left / Progress, richer stats, smarter lists, deeper integrations, and the daily-driver polish former Trakt users usually miss.

[Demo](https://yamtrack.dannyvfilms.com) | [Docker Image](https://github.com/dannyvfilms/Yamtrack/pkgs/container/yamtrack) | [Releases](https://github.com/dannyvfilms/Yamtrack/releases)

Credentials for Demo:
- Username: `demo`
- Password: `demodemo`

## Why choose this fork?

Upstream Yamtrack is the foundation. This fork is the stronger pick if you want a more opinionated, Trakt-like day-to-day experience instead of a mostly stock Yamtrack install.

- Trakt-style **Time Left / Progress** workflow with runtime-aware TV sorting, dropped-show fixes, and time-watched views
- Unified **History** page with month navigation, media-type and genre filters, cached refreshes, and inline cleanup for duplicate or extra plays
- Rich **statistics** with all-time and custom ranges, Top Played, media-hours cards, reading stats, music and podcast stats, person pages, and comparison tooling
- Better **lists** with public profiles, recommendations, smart lists, list tags, drag-and-drop ordering, release-date sorting, completion indicators, RSS/JSON feeds, and backup export/import
- More complete **media coverage** with music, podcasts, board games, percentage-based reading progress, game lengths, author pages, person credits, localized titles, and TVDB-backed grouped anime handling
- **Collection and owned-media** support with filters, season rollups, track/episode collection state, background metadata fetching, and richer detail pages
- More useful **discover** recommendations with caching, personalization, feedback, better TV/anime planning, and Trakt popularity signals
- Broader **integrations** across Trakt, Plex, Jellyfin, Jellyseerr, Pocket Casts, Last.fm, Audiobookshelf, TVDB, Steam, and more
- Stronger **mobile and PWA** behavior with compact/comfortable grids, quick season updates, iOS fixes, better touch handling, and more readable cards/details
- Better **daily performance and reliability** for large libraries via runtime caching, history/stat cache layers, SQLite lock hardening, startup guards, and large-query optimizations

## What this fork adds

- **Progress and history**: Time Left sorting, time-watched sorting, live Now Playing card, episode ratings, filtered history, delete flows, duplicate-play cleanup, and better subtitle/context on media details.
- **Statistics and insight**: cached refreshable stats, Top Played, hours-by-media cards, reading stats, music/podcast stats, person and actor pages, rating-scale-aware averages, and comparison tooltips.
- **Lists and sharing**: public/private lists, public profiles, smart list builder, list tags, list recommendations, release-date sort, drag-and-drop ordering, completion indicators, RSS/JSON feeds, and list export/import in scheduled backups.
- **Media depth**: collection metadata, localized and alternate titles, author profiles, person credits, runtime chips, game lengths, watch-provider region preference, and direct provider-ID search.
- **Integrations and imports**: Trakt watch history and lists, Trakt watchlist import, Plex watchlist sync, Plex ratings sync, Pocket Casts sync, Last.fm history/scrobbling, Audiobookshelf imports, Jellyfin webhooks, Jellyseerr auto-add, TVDB metadata, and safer Steam updates.
- **Daily-use polish**: mobile grid preferences, quick season update button, subtitle display preference, date/time formatting, rating scale preference, built-in demo account, large-library speedups, better cache refresh UX, and iOS/SQLite resiliency.

## Built for former Trakt users

If you left Trakt because the daily workflow got worse, this fork is built around the workflows people keep asking for instead of treating them as minor extras.

- **Better progress workflow**: a Trakt-style Progress view with Time Left, runtime-aware sorting, and smarter handling for in-progress, planning, completed, and dropped shows.
- **Better history browsing**: a single history feed across watches and listens, with month-based navigation, filters, extra-play cleanup, and detail-page shortcuts.
- **Better recap-style stats**: cached year-in-review and all-time views with media hours, top played, streaks, comparisons, and per-media breakdowns that are actually fun to browse.
- **Better list sharing and exports**: shareable public lists, public profiles, recommendations, RSS/JSON feeds, backup exports, and Trakt list/watchlist import support.
- **Better all-in-one tracking**: keep movies, TV, anime, music, podcasts, books, comics, manga, games, and board games in one app instead of splitting your tracking across multiple tools.
- **Better daily polish than upstream for this use case**: more preferences, better mobile behavior, stronger large-library performance, and more opinionated UX for people who use the app every day.

## What's different in practice

### Time Left / Progress

Trakt-style Progress is one of the biggest reasons to pick this fork: it mixes active and planned shows more usefully, uses real runtime data, and makes backlog decisions faster.

<img alt="Time Left progress view" src="https://github.com/user-attachments/assets/ab5594ea-6ddc-4512-9837-87b68ec874c2" />

### History

The History page is built to answer "what did I actually watch or listen to?" without bouncing across detail pages or separate media-type views.

<img alt="History page" src="https://github.com/user-attachments/assets/18927954-dd57-40ba-86ef-11ae986cf9ee" />

### Statistics

This fork leans hard into recap-style browsing with cached, range-based statistics that feel closer to the way former Trakt users evaluate their media habits.

<img alt="Statistics dashboard" src="https://github.com/user-attachments/assets/be9da320-d745-45aa-8c6a-23efa66d0c6c" />

### Shareable Lists

Lists are more than personal bookmarks here: you can share them publicly, recommend from them, expose them on profiles, and build smarter list workflows around them.

<img alt="Shareable lists" src="https://github.com/user-attachments/assets/ef13eff1-dc14-4ab6-b5d0-39598a1264ec" />

### Collections / Owned Media

Collection support adds owned-media context, richer detail views, format metadata, and season-level rollups so the app tracks what you have as well as what you watched.

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

This fork expands Yamtrack into a more complete all-in-one tracker with artist, album, track, podcast show, and podcast episode workflows instead of leaving those to separate apps.

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

## More features you'll actually notice day to day

- **Media details feel richer**: action popovers, better subtitles, provider branding, score chips, runtime chips, localized titles, notes previews, and cleaner mobile layouts.
- **Filters are much stronger**: remembered filters, combined rating/collection filters, tag filters, release-state filters, media-type filters, genre filters, and better toolbars.
- **Lists behave better on mobile and desktop**: smarter chips, faster pages, backdrop covers, count fixes, clearer completion visibility, and cleaner public views.
- **Books and reading are less second-class**: barcode scanning, percentage progress, top authors, author pages, and harder-to-break imports.
- **Imports and webhooks are less fragile**: safer dedupe logic, fallback ID handling, better logging, and fixes for edge cases across Trakt, Plex, Pocket Casts, Jellyfin, Audiobookshelf, and Steam.
- **The app feels better under load**: faster large-library lists, cached history/stat refreshes, local runtime backfills, and better handling for SQLite-specific failure modes.

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
   celery -A config worker --beat --scheduler django --loglevel DEBUG
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
