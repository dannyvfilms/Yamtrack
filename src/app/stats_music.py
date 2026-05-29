import datetime
from collections import defaultdict

from app import config
from app.models import MediaTypes
from app.statistics_cache import STATISTICS_TOP_N


def _collect_music_play_data(music_queryset, start_date, end_date):
    """Collect music play datetimes and per-play runtime from history records.

    Returns:
        tuple: (list of datetimes, list of (music_entry, datetime, runtime_minutes) tuples)
    """
    from app.statistics import _get_music_runtime_minutes, _localize_datetime

    datetimes = []
    play_details = []  # (music_entry, datetime, runtime_minutes)

    if music_queryset is None:
        return datetimes, play_details

    for music in music_queryset:
        runtime_minutes = _get_music_runtime_minutes(music)

        # Get all history records ordered by history_date (oldest first)
        history_records = list(music.history.all().order_by("history_date"))

        # Group history records by end_date to deduplicate
        # Each unique end_date represents one play, even if there are multiple history records
        plays_by_end_date = {}  # end_date -> (history_record, history_date)

        for history_record in history_records:
            history_end_date = getattr(history_record, "end_date", None)
            history_date = getattr(history_record, "history_date", None)

            # Skip records without end_date (not a completed play)
            if not history_end_date or not history_date:
                continue

            # If we haven't seen this end_date, or this history_record is closer to the end_date,
            # use this one as the canonical record for this play
            if history_end_date not in plays_by_end_date:
                plays_by_end_date[history_end_date] = (history_record, history_date)
            else:
                # Prefer the history record where history_date is closest to end_date
                # (within reason - if history_date is way after end_date, it's likely a metadata update)
                existing_history_date = plays_by_end_date[history_end_date][1]
                time_diff_existing = abs((existing_history_date - history_end_date).total_seconds())
                time_diff_current = abs((history_date - history_end_date).total_seconds())

                # Prefer the one closer to end_date, but only if it's within 24 hours
                # (metadata updates can happen days/weeks later)
                if time_diff_current < time_diff_existing and time_diff_current < 86400:  # 24 hours
                    plays_by_end_date[history_end_date] = (history_record, history_date)

        # Process unique plays within date range
        for play_end_date, (history_record, _) in plays_by_end_date.items():
            # Check if within date range
            if start_date and end_date:
                if not (start_date <= play_end_date <= end_date):
                    continue

            localized_date = _localize_datetime(play_end_date)
            datetimes.append(localized_date)
            play_details.append((music, localized_date, runtime_minutes))

    return datetimes, play_details


def _compute_music_top_lists(play_details, limit=5):
    """Compute top artists, albums, and tracks by total listening time.

    Args:
        play_details: List of (music_entry, datetime, runtime_minutes) tuples
        limit: Number of items to return per list

    Returns:
        dict with top_artists, top_albums, top_tracks lists
    """
    from app.helpers import minutes_to_hhmm

    # Aggregate by artist, album, and track
    artist_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": "", "image": "", "id": None})
    album_stats = defaultdict(
        lambda: {
            "minutes": 0,
            "plays": 0,
            "title": "",
            "artist": "",
            "artist_id": None,
            "artist_name": "",
            "image": "",
            "id": None,
        },
    )
    track_stats = defaultdict(
        lambda: {
            "minutes": 0,
            "plays": 0,
            "title": "",
            "artist": "",
            "album": "",
            "album_image": "",
            "album_id": None,
            "album_artist_id": None,
            "album_artist_name": "",
            "id": None,
        },
    )

    for music, dt, runtime in play_details:
        # Track stats (use music.id as key since each Music is a unique track entry)
        track_key = music.id
        track_stats[track_key]["minutes"] += runtime
        track_stats[track_key]["plays"] += 1
        track_stats[track_key]["title"] = music.item.title if music.item else "Unknown"
        track_stats[track_key]["id"] = music.id

        # Prefer the explicit music.artist link, but fall back to album.artist so
        # canonical artist/album URLs can still be built from rolled-up stats data.
        album = music.album
        artist = music.artist or getattr(album, "artist", None)

        if artist:
            track_stats[track_key]["artist"] = artist.name
            artist_stats[artist.id]["minutes"] += runtime
            artist_stats[artist.id]["plays"] += 1
            artist_stats[artist.id]["name"] = artist.name
            artist_stats[artist.id]["image"] = artist.image or ""
            artist_stats[artist.id]["id"] = artist.id

        if album:
            track_stats[track_key]["album"] = album.title
            track_stats[track_key]["album_image"] = album.image or track_stats[track_key]["album_image"]
            track_stats[track_key]["album_id"] = album.id
            track_stats[track_key]["album_artist_id"] = artist.id if artist else None
            track_stats[track_key]["album_artist_name"] = artist.name if artist else ""
            album_stats[album.id]["minutes"] += runtime
            album_stats[album.id]["plays"] += 1
            album_stats[album.id]["title"] = album.title
            album_stats[album.id]["artist"] = artist.name if artist else "Unknown"
            album_stats[album.id]["artist_id"] = artist.id if artist else None
            album_stats[album.id]["artist_name"] = artist.name if artist else ""
            album_stats[album.id]["image"] = album.image or ""
            album_stats[album.id]["id"] = album.id

    # Sort by minutes and take top N
    top_artists = sorted(artist_stats.values(), key=lambda x: x["minutes"], reverse=True)[:limit]
    top_albums = sorted(album_stats.values(), key=lambda x: x["minutes"], reverse=True)[:limit]
    top_tracks = sorted(track_stats.values(), key=lambda x: x["minutes"], reverse=True)[:limit]

    album_artist_lookup = {
        album_id: {
            "artist_id": values.get("artist_id"),
            "artist_name": values.get("artist_name"),
        }
        for album_id, values in album_stats.items()
        if values.get("artist_id") is not None or values.get("artist_name")
    }

    for album_item in top_albums:
        artist_data = album_artist_lookup.get(album_item.get("id"))
        if not artist_data:
            continue
        if album_item.get("artist_id") is None:
            album_item["artist_id"] = artist_data.get("artist_id")
        if not album_item.get("artist_name"):
            album_item["artist_name"] = artist_data.get("artist_name", "")

    for track_item in top_tracks:
        artist_data = album_artist_lookup.get(track_item.get("album_id"))
        if not artist_data:
            continue
        if track_item.get("album_artist_id") is None:
            track_item["album_artist_id"] = artist_data.get("artist_id")
        if not track_item.get("album_artist_name"):
            track_item["album_artist_name"] = artist_data.get("artist_name", "")

    # Format durations
    for item in top_artists + top_albums + top_tracks:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])

    return {
        "top_artists": top_artists,
        "top_albums": top_albums,
        "top_tracks": top_tracks,
    }


# Country name mapping (ISO 3166-1 alpha-2 -> English name)
COUNTRY_NAME_MAP = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AI": "Anguilla",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AQ": "Antarctica",
    "AR": "Argentina",
    "AS": "American Samoa",
    "AT": "Austria",
    "AU": "Australia",
    "AW": "Aruba",
    "AX": "Aland Islands",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BL": "Saint Barthelemy",
    "BM": "Bermuda",
    "BN": "Brunei Darussalam",
    "BO": "Bolivia, Plurinational State of",
    "BQ": "Bonaire, Sint Eustatius and Saba",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BV": "Bouvet Island",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CC": "Cocos (Keeling) Islands",
    "CD": "Congo, Democratic Republic of the",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Cote d'Ivoire",
    "CK": "Cook Islands",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cabo Verde",
    "CW": "Curacao",
    "CX": "Christmas Island",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "EH": "Western Sahara",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FK": "Falkland Islands (Malvinas)",
    "FM": "Micronesia, Federated States of",
    "FO": "Faroe Islands",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom of Great Britain and Northern Ireland",
    "GD": "Grenada",
    "GE": "Georgia",
    "GF": "French Guiana",
    "GG": "Guernsey",
    "GH": "Ghana",
    "GI": "Gibraltar",
    "GL": "Greenland",
    "GM": "Gambia",
    "GN": "Guinea",
    "GP": "Guadeloupe",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GS": "South Georgia and the South Sandwich Islands",
    "GT": "Guatemala",
    "GU": "Guam",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HM": "Heard Island and McDonald Islands",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IM": "Isle of Man",
    "IN": "India",
    "IO": "British Indian Ocean Territory",
    "IQ": "Iraq",
    "IR": "Iran, Islamic Republic of",
    "IS": "Iceland",
    "IT": "Italy",
    "JE": "Jersey",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KI": "Kiribati",
    "KM": "Comoros",
    "KN": "Saint Kitts and Nevis",
    "KP": "Korea, Democratic People's Republic of",
    "KR": "Korea, Republic of",
    "KW": "Kuwait",
    "KY": "Cayman Islands",
    "KZ": "Kazakhstan",
    "LA": "Lao People's Democratic Republic",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova, Republic of",
    "ME": "Montenegro",
    "MF": "Saint Martin (French part)",
    "MG": "Madagascar",
    "MH": "Marshall Islands",
    "MK": "North Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MO": "Macao",
    "MP": "Northern Mariana Islands",
    "MQ": "Martinique",
    "MR": "Mauritania",
    "MS": "Montserrat",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NC": "New Caledonia",
    "NE": "Niger",
    "NF": "Norfolk Island",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands, Kingdom of the",
    "NO": "Norway",
    "NP": "Nepal",
    "NR": "Nauru",
    "NU": "Niue",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PF": "French Polynesia",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PM": "Saint Pierre and Miquelon",
    "PN": "Pitcairn",
    "PR": "Puerto Rico",
    "PS": "Palestine, State of",
    "PT": "Portugal",
    "PW": "Palau",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RE": "Reunion",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russian Federation",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SB": "Solomon Islands",
    "SC": "Seychelles",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SH": "Saint Helena, Ascension and Tristan da Cunha",
    "SI": "Slovenia",
    "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SM": "San Marino",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "South Sudan",
    "ST": "Sao Tome and Principe",
    "SV": "El Salvador",
    "SX": "Sint Maarten (Dutch part)",
    "SY": "Syrian Arab Republic",
    "SZ": "Eswatini",
    "TC": "Turks and Caicos Islands",
    "TD": "Chad",
    "TF": "French Southern Territories",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TK": "Tokelau",
    "TL": "Timor-Leste",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TO": "Tonga",
    "TR": "Turkiye",
    "TT": "Trinidad and Tobago",
    "TV": "Tuvalu",
    "TW": "Taiwan, Province of China",
    "TZ": "Tanzania, United Republic of",
    "UA": "Ukraine",
    "UG": "Uganda",
    "UM": "United States Minor Outlying Islands",
    "US": "United States of America",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VA": "Holy See",
    "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela, Bolivarian Republic of",
    "VG": "Virgin Islands (British)",
    "VI": "Virgin Islands (U.S.)",
    "VN": "Viet Nam",
    "VU": "Vanuatu",
    "WF": "Wallis and Futuna",
    "WS": "Samoa",
    "YE": "Yemen",
    "YT": "Mayotte",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}


def _country_name_from_code(code: str) -> str:
    """Return the full country name for an ISO alpha-2 code."""
    if not code:
        return "Unknown"
    code = str(code).upper()
    return COUNTRY_NAME_MAP.get(code, code)


def _parse_release_date_str(date_str):
    """Parse a MusicBrainz date string (YYYY, YYYY-MM, YYYY-MM-DD) to date."""
    if not date_str:
        return None
    try:
        if len(date_str) >= 10:
            return datetime.datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        if len(date_str) == 7:
            return datetime.datetime.strptime(date_str, "%Y-%m").date()
        if len(date_str) == 4:
            return datetime.datetime.strptime(date_str, "%Y").date()
    except ValueError:
        return None
    return None


def _hydrate_music_metadata_for_rollups(music_queryset):
    """Ensure artists/albums have genres/country/release_date without manual visits.

    Currently we only use locally stored metadata to avoid extra provider calls.
    """
    # No-op placeholder: relies on metadata stored at creation/sync time.


def _compute_music_top_rollups(play_details, limit=5):
    """Compute top genres, decades, and countries from music play details."""
    from app.helpers import minutes_to_hhmm
    from app.statistics import _coerce_genre_list

    genre_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})
    decade_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "label": ""})
    country_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "code": ""})

    for music, dt, runtime in play_details:
        minutes = runtime or 0

        # Genres: prefer album genres, fall back to artist genres
        genres = []
        if getattr(music, "album", None) and music.album.genres:
            genres = _coerce_genre_list(music.album.genres)
        elif getattr(music, "artist", None) and music.artist.genres:
            genres = _coerce_genre_list(music.artist.genres)
        elif getattr(music, "track", None) and music.track.genres:
            genres = _coerce_genre_list(music.track.genres)

        for genre in genres:
            key = str(genre).title()
            genre_stats[key]["minutes"] += minutes
            genre_stats[key]["plays"] += 1
            genre_stats[key]["name"] = key

        # Decades: from album release_date if available
        release_date = getattr(music.album, "release_date", None) if getattr(music, "album", None) else None
        if release_date and release_date.year:
            decade_label = f"{(release_date.year // 10) * 10}s"
            decade_stats[decade_label]["minutes"] += minutes
            decade_stats[decade_label]["plays"] += 1
            decade_stats[decade_label]["label"] = decade_label

        # Countries: from artist.country
        country_code = ""
        if getattr(music, "artist", None) and music.artist.country:
            country_code = music.artist.country
        if country_code:
            code_upper = country_code.upper()
            country_stats[code_upper]["minutes"] += minutes
            country_stats[code_upper]["plays"] += 1
            country_stats[code_upper]["code"] = code_upper
            country_stats[code_upper]["name"] = _country_name_from_code(code_upper)

    def _format_top(stat_map, label_key):
        items = sorted(
            stat_map.values(),
            key=lambda x: (x["minutes"], x["plays"]),
            reverse=True,
        )[:limit]
        for item in items:
            item["formatted_duration"] = minutes_to_hhmm(item["minutes"])
        return items

    return {
        "top_genres": _format_top(genre_stats, "name"),
        "top_decades": _format_top(decade_stats, "label"),
        "top_countries": _format_top(country_stats, "code"),
    }


def get_music_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for music activity.

    This is similar to TV/Movie consumption stats but uses minutes instead of hours
    and includes top artists, albums, and tracks.
    """
    from app.statistics import (
        _build_media_charts,
        _compute_metric_breakdown,
        calculate_minutes_per_media_type,
    )

    music_queryset = (user_media or {}).get(MediaTypes.MUSIC.value)

    # Prefetch related data for efficiency
    # Note: history manager from simple_history cannot be prefetched, so we access it directly in the loop
    # Clear any existing prefetches that might include 'history' (which can't be prefetched)
    if music_queryset is not None:
        # Get the model and recreate queryset to avoid any problematic prefetches
        model = music_queryset.model
        # Get the IDs from the original queryset
        music_ids = list(music_queryset.values_list("id", flat=True))
        if music_ids:
            # Recreate queryset with only safe prefetches
            music_queryset = model.objects.filter(id__in=music_ids).select_related("item", "artist", "album")
        else:
            music_queryset = None

    music_datetimes, play_details = _collect_music_play_data(music_queryset, start_date, end_date)

    # Hydrate missing metadata (genres, country, release_date) from stored data only (no provider calls)
    if music_queryset is not None:
        _hydrate_music_metadata_for_rollups(music_queryset)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.MUSIC.value, 0)
    total_plays = len(music_datetimes)

    # For music, we use minutes breakdown instead of hours
    minutes_breakdown = _compute_metric_breakdown(
        total_minutes,
        music_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        music_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.MUSIC.value)
    chart_label = "Music Plays"
    charts = _build_media_charts(music_datetimes, color, chart_label)

    # Compute top lists
    top_lists = _compute_music_top_lists(play_details, limit=STATISTICS_TOP_N)
    meta_lists = _compute_music_top_rollups(play_details, limit=STATISTICS_TOP_N)

    return {
        "minutes": minutes_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_artists": top_lists["top_artists"],
        "top_albums": top_lists["top_albums"],
        "top_tracks": top_lists["top_tracks"],
        "top_genres": meta_lists["top_genres"],
        "top_decades": meta_lists["top_decades"],
        "top_countries": meta_lists["top_countries"],
    }
