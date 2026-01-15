# Yamtrack

![App Tests](https://github.com/FuzzyGrim/Yamtrack/actions/workflows/app-tests.yml/badge.svg)
![Docker Image](https://github.com/FuzzyGrim/Yamtrack/actions/workflows/docker-image.yml/badge.svg)
![CodeFactor](https://www.codefactor.io/repository/github/fuzzygrim/yamtrack/badge)
![Codecov](https://codecov.io/github/FuzzyGrim/Yamtrack/branch/dev/graph/badge.svg?token=PWUG660120)
![GitHub](https://img.shields.io/badge/license-AGPL--3.0-blue)

Yamtrack is a self hosted media tracker for movies, tv shows, music, anime, manga, video games, books, comics, and board games. 

## 📱 Repo Specific Features

This fork keeps Yamtrack’s core feel, but leans hard into *daily usability*: faster navigation on large libraries, more “what should I watch next?” tooling, and a bunch of stability/performance work that makes the app feel snappier—especially on mobile and as a PWA.

### For former Trakt users (quick visual tour)

- [Time Left Sort](https://ibb.co/yFr6dSDM): Trakt-style “Progress” page: mixes *In Progress* + *Planning*, then *Completed*, then *Dropped*, sorted by time left.
- [History Page](https://ibb.co/tp51QHFF): A clean, single place to see *everything* you did—without hopping between media types or digging through detail pages.
- [View Additional Plays](https://ibb.co/jvdswfHF): Drill into extra plays to understand a show/genre’s history, and quickly spot/remove duplicate plays.
- [Activity Overview Statistics](https://ibb.co/r2cTxgcX): Year-in-review + all-time style stats with the kind of depth Trakt users expect.
- [Sharable Lists](https://ibb.co/JjwX9hNX): Share your best lists, keep the link simple, and get recommendations based on what you share.
- [More Media Types](https://ibb.co/C3d3HMxD): Beyond the originals: music + podcasts (and more), so your tracking isn’t split across multiple apps.

### Major additions (beyond upstream)

- **Music + Podcast tracking (real library support)**
  - Albums / artists / tracks, plus scrobbling + metadata integrations (MusicBrainz, Plex music, artwork fetching)
  - Podcast shows + episodes, RSS support, Pocket Casts import/sync, and better completion inference

- **History page rewrite**
  - Replaced media timeline in statistics with a dedicated page
  - Month-based navigation + per-day caching so big histories load quickly
  - Background refresh + status polling, plus fixes for iOS/PWA refresh loops
  - Supports filters for Today in History, Genres, and more

- **Statistics page overhaul**
  - Major additions in the types of statistics reported to match Trakt's year in review
  - Cached stats with multiple time ranges, richer breakdowns, and better “what did I actually do?” views
  - More useful distributions (including time-based views), streak tooling, and mobile-friendly layouts

- **Lists + recommendations**
  - Share your lists publicly with a Cloudflare Tunnel, Reverse Proxy, or similar workflow
  - Better list detail experiences (sorting, filtering, searching)
  - Recommendation workflow integrated into lists so lists can *generate ideas*, not just store them

- **Integrations that behave better under real usage**
  - Improved Plex import/webhook handling (including edge cases like GUID quirks and SQLite locking)
  - Scheduled Pocket Casts imports and more resilient background workflows

### Quality-of-life upgrades you feel every day

- **Backlog-friendly TV sorting**: Time Left sorting helps you finish what’s closest to “done,” not what’s alphabetically next.
- **Mobile-first tweaks**: compact vs comfortable grids, more readable cards, fewer tap-fights with overlays/z-index issues.
- **Preferences that matter**: enable/disable media types, date/time formats, sort defaults, mobile layout choices, auto-pause behaviors.
- **Cleaner tracking + data integrity**: runtime population/backfills, validation improvements, and fewer “why is this weird?” moments.
- **Performance & stability**: caching infrastructure (history + stats), safer refresh behavior, and fewer long-load pages.

### A couple of feature screenshots

**History**
<img src="https://github.com/user-attachments/assets/e60ab087-5faa-4cc0-ad15-f1865453ec6e" />
<img src="https://github.com/user-attachments/assets/72338fdf-bb24-4c82-92ca-828bd2c1820b" />

**Statistics**
<img src="https://github.com/user-attachments/assets/5e7b301a-3a92-4c0b-a7d6-573bca240058" />
<img src="https://github.com/user-attachments/assets/04853bea-f5d9-4b71-9ea2-0f9414479f4e" />

## 🚀 Repo Specific Demo

You can try the app at [yamtrack.dannyvfilms.com](https://yamtrack.dannyvfilms.com) using the username `demo` and password `demodemo`.

## 📱 Repo Specific Installation
Docker image is now available: ```docker pull ghcr.io/dannyvfilms/yamtrack:latest```

Available tags:
- `:latest` - Points to the stable release branch (default when no tag is specified)
- `:release` - Explicit tag for the release branch
- `:dev` - Exact copy of upstream repo (FuzzyGrim/Yamtrack)

Original Readme below, needs to be updated in several aspects.

## 🚀 Demo

You can try the app at [yamtrack.fuzzygrim.com](https://yamtrack.fuzzygrim.com) using the username `demo` and password `demo`.

## ✨ Features

- 🎬 Track movies, tv shows, anime, manga, games, books, comics, and board games.
- 📺 Track each season of a tv show individually and episodes watched.
- ⭐ Save score, status, progress, repeats (rewatches, rereads...), start and end dates, or write a note.
- 📈 Keep a tracking history with each action with a media, such as when you added it, when you started it, when you started watching it again, etc.
- ✏️ Create custom media entries, for niche media that cannot be found by the supported APIs.
- 📂 Create personal lists to organize your media for any purpose, add other members to collaborate on your lists.
- 📅 Keep up with your upcoming media with a calendar, which can be subscribed to in external applications using a iCalendar (.ics) URL.
- 🔔 Receive notifications of upcoming releases via Apprise (supports Discord, Telegram, ntfy, Slack, email, and many more).
- 🐳 Easy deployment with Docker via docker-compose with SQLite or PostgreSQL.
- 👥 Multi-users functionality allowing individual accounts with personalized tracking.
- 🔑 Flexible authentication options including OIDC and 100+ social providers (Google, GitHub, Discord, etc.) via django-allauth.
- 🦀 Integration with [Jellyfin](https://jellyfin.org/), [Plex](https://plex.tv/) and [Emby](https://emby.media/) to automatically track new media watched.
- 📥 Import from [Trakt](https://trakt.tv/), [Simkl](https://simkl.com/), [MyAnimeList](https://myanimelist.net/), [AniList](https://anilist.co/) and [Kitsu](https://kitsu.app/) with support for periodic automatic imports.
- 📊 Export all your tracked media to a CSV file and import it back.

## 📱 Screenshots

| Homepage                                                                                       | Calendar                                                                                    |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/homepage.png?v2" alt="Homepage" /> | <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/calendar.png" alt="calendar" /> |

| Media List Grid                                                                                    | Media List Table                                                                                     |
| -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/medialist_grid.png" alt="List Grid" /> | <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/medialist_table.png" alt="List Table" /> |

| Media Details                                                                                         | Tracking                                                                                    |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/media_details.png" alt="Media Details" /> | <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/tracking.png" alt="Tracking" /> |

| Season Details                                                                                          | Tracking Episodes                                                                                            |
| ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/season_details.png" alt="Season Details" /> | <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/tracking_episode.png" alt="Tracking Episodes" /> |

| Lists                                                                                 | Statistics                                                                                      |
| ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/lists.png" alt="Lists" /> | <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/statistics.png" alt="Statistics" /> |

| Create Manual Entries                                                                                         | Import Data                                                                                       |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/create_custom.png" alt="Create Manual Entries" /> | <img src="https://cdn.fuzzygrim.com/file/fuzzygrim/yamtrack/import_data.png" alt="Import Data" /> |

## 🐳 Installing with Docker

Copy the default `docker-compose.yml` file from the repository and set the environment variables. This would use a SQlite database, which is enough for most use cases.

The Docker image can be used with or without a tag:
- `ghcr.io/dannyvfilms/yamtrack` (defaults to `:latest`)
- `ghcr.io/dannyvfilms/yamtrack:latest` (explicit latest tag)
- `ghcr.io/dannyvfilms/yamtrack:release` (explicit release tag)

To start the containers run:

```bash
docker-compose up -d
```

Alternatively, if you need a PostgreSQL database, you can use the `docker-compose.postgres.yml` file.

### 🌊 Reverse Proxy Setup

When using a reverse proxy, if you see a `403 - Forbidden` error, you need to set the `URLS` environment variable to the URL you are using for the app.

```bash
services:
  yamtrack:
    ...
    environment:
      - URLS=https://yamtrack.mydomain.com
    ...
```

Note that the setting must include the correct protocol (`https` or `http`), and must not include the application `/` context path. Multiple origins can be specified by separating them with a comma (`,`).

### ⚙️ Environment variables

For detailed information on environment variables, please refer to the [Environment Variables wiki page](https://github.com/FuzzyGrim/Yamtrack/wiki/Environment-Variables).

## 💻 Local development

Clone the repository and change directory to it.

```bash
git clone https://github.com/FuzzyGrim/Yamtrack.git
cd Yamtrack
```

Install Redis or spin up a bare redis container:

```bash
docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:8-alpine
```

Create a `.env` file in the root directory and add the following variables.

```bash
TMDB_API=API_KEY
MAL_API=API_KEY
IGDB_ID=IGDB_ID
IGDB_SECRET=IGDB_SECRET
STEAM_API_KEY=STEAM_API_SECRET
SECRET=SECRET
DEBUG=True
```

Then run the following commands.

```bash
python -m pip install -U -r requirements-dev.txt
cd src
python manage.py migrate
python manage.py runserver & celery -A config worker --beat --scheduler django --loglevel DEBUG & tailwindcss -i ./static/css/input.css -o ./static/css/tailwind.css --watch
```

Go to: http://localhost:8000

## 💪 Support the Project

There are many ways you can support Yamtrack's development:

### ⭐ Star the Project

The simplest way to show your support is to star the repository on GitHub. It helps increase visibility and shows appreciation for the work.

### 🐛 Bug Reports

Found a bug? Open an [issue](https://github.com/FuzzyGrim/Yamtrack/issues) on GitHub with detailed steps to reproduce it. Quality bug reports are incredibly valuable for improving stability.

### 💡 Feature Suggestions

Have ideas for new features? Share them through [GitHub issues](https://github.com/FuzzyGrim/Yamtrack/issues). Your feedback helps shape the future of Yamtrack.

### 🧪 Contributing

Pull requests are welcome! Whether it's fixing typos, improving documentation, or adding new features, your contributions help make Yamtrack better for everyone.

### ☕ Donate

If you'd like to support the project financially:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/fuzzygrim)
