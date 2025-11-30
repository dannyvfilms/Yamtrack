# Yamtrack

![App Tests](https://github.com/FuzzyGrim/Yamtrack/actions/workflows/app-tests.yml/badge.svg)
![Docker Image](https://github.com/FuzzyGrim/Yamtrack/actions/workflows/docker-image.yml/badge.svg)
![CodeFactor](https://www.codefactor.io/repository/github/fuzzygrim/yamtrack/badge)
![Codecov](https://codecov.io/github/FuzzyGrim/Yamtrack/branch/dev/graph/badge.svg?token=PWUG660120)
![GitHub](https://img.shields.io/badge/license-AGPL--3.0-blue)

Yamtrack is a self hosted media tracker for movies, tv shows, anime, manga, video games and books.

## 📱 Repo Specific Features

| Sort Lists by Ratings                                                                                       | Preferences Tab for Optional Features                                                                                    |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| <img alt="Screenshot 2025-11-13 at 8 45 17 PM" src="https://github.com/user-attachments/assets/77eb1685-d709-485f-a79b-2b4de2698009" /> | <img width="1176" height="583" alt="Screenshot 2025-11-13 at 8 46 54 PM" src="https://github.com/user-attachments/assets/0368c01b-a10c-44a9-9ed1-aa150b78f3aa" /> |


| Top 10 Statistics                                                                                    | Activity Overview Statistics                                                                                     |
| -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| <img alt="Screenshot 2025-11-13 at 8 48 32 PM" src="https://github.com/user-attachments/assets/9150add3-c7dd-4a18-a422-fcc67c30bb37" /> | <img alt="Screenshot 2025-11-13 at 8 49 48 PM" src="https://github.com/user-attachments/assets/91d85919-30c9-4a9d-b65b-92c4e78f8c5c" /> |

| Combined Duplicates                                                                                         | Sort by Time Left                                                                                    |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| <img alt="Screenshot 2025-11-13 at 8 51 18 PM" src="https://github.com/user-attachments/assets/4d3c2796-90c7-47fe-b23e-c8104fff92cf" /> | <img alt="Screenshot 2025-11-13 at 8 52 39 PM" src="https://github.com/user-attachments/assets/3966fe33-9f9d-4d4d-bf35-1cbf61ac2e90" /> |

| Refined Mobile Layout Changes                                                                                         | Optionally Convert In Progress to Paused after Delay                                                                                    |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| <img alt="Screenshot 2025-11-13 at 8 51 18 PM" src="https://github.com/user-attachments/assets/3f0ad1dd-8121-4d31-8641-a6e60fc9d4c4" /> | <img alt="Screenshot 2025-11-15 at 8 17 50 PM" src="https://github.com/user-attachments/assets/8222884e-ac1e-417e-be14-fa744918755a" /> |
| Added History Page                                                                                         | Placeholder for Second Screenshot                                                                                    |
| <img  alt="Screenshot 2025-11-26 at 6 12 33 PM" src="https://github.com/user-attachments/assets/65ae9a2c-ad33-4129-85e4-4653ef48658a" />
 | <img/> |


## 📱 Repo Specific Installation
Docker image is now available: ```docker pull ghcr.io/dannyvfilms/yamtrack:release```

I haven't figured out the workflow to publish Docker containers yet. Until then, use the following commands to build locally:

```bash
cd /mnt/users/appdata
git clone https://github.com/dannyvfilms/Yamtrack.git
cd Yamtrack

cat > .env <<EOF
TMDB_API=CHANGE_ME
IGDB_ID=CHANGE_ME
IGDB_SECRET=CHANGE_ME
SECRET=$(openssl rand -hex 32)
DEBUG=FALSE
REDIS_URL=CHANGE_ME
ALLOWED_HOSTS=CHANGE_ME
EOF

git checkout release
git pull origin release
docker compose build --no-cache
docker compose up -d
```

To update your container:
```
cd /mnt/users/appdata/Yamtrack
git checkout release
git pull origin release
docker compose build --no-cache
```
Then recreate your container with the new image (e.g. "Recreate" in Portainer).

## 🚀 Demo

You can try the app at [yamtrack.fuzzygrim.com](https://yamtrack.fuzzygrim.com) using the username `demo` and password `demo`.

## ✨ Features

- 🎬 Track movies, tv shows, anime, manga, games, books and comics.
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
