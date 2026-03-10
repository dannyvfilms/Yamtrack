"""Discover row registry."""

from __future__ import annotations

from app.discover.schemas import RowDefinition
from app.models import MediaTypes

ALL_MEDIA_KEY = "all"
DISCOVER_MEDIA_TYPES = [
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.MUSIC.value,
    MediaTypes.PODCAST.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOARDGAME.value,
]

ROW_REGISTRY: dict[str, list[RowDefinition]] = {
    ALL_MEDIA_KEY: [],
    MediaTypes.MOVIE.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What everyone has been watching this week.", "trakt"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Seen", "Canon", "Must-watch classics still missing", "trakt"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming releases to watchlist", "trakt"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you movies tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.TV.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What everyone has been watching this week.", "trakt"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Seen", "Canon", "Must-watch classics still missing", "trakt"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming releases to watchlist", "trakt"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you shows tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("clear_out_next", "Clear Out Next", "Momentum", "Your in-progress shows ranked by what best fits your current phase.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.ANIME.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What anime fans have been watching this week.", "trakt"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Seen", "Canon", "Must-watch anime still missing", "trakt"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming anime to watchlist", "trakt"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you anime tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("clear_out_next", "Clear Out Next", "Momentum", "Your in-progress anime ranked by what best fits your current phase.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.MUSIC.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What listeners have been spinning this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Heard", "Canon", "Essential listens still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming releases to queue next", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you tracks tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.PODCAST.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What podcast listeners have been playing this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Heard", "Canon", "Must-hear shows still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming launches to watchlist", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you podcasts tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.BOOK.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What readers have been picking up this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Read", "Canon", "Must-read classics still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming releases to watchlist", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you books tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.COMIC.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What comic readers are following this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Read", "Canon", "Must-read runs still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming issues and volumes to watchlist", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you comics tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.MANGA.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What manga readers have been into this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Read", "Canon", "Must-read manga still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming manga to watchlist", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you manga tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.GAME.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What players have been jumping into this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Played", "Canon", "Must-play classics still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming launches to watchlist", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you games tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.BOARDGAME.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What tabletop players are buzzing about this week.", "provider"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Played", "Canon", "Foundational games still missing", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming releases to watchlist", "provider"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you board games tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
}


def is_supported_media_type(media_type: str) -> bool:
    """Return True when Discover supports the selected media type."""
    return media_type == ALL_MEDIA_KEY or media_type in ROW_REGISTRY


def get_rows(media_type: str, include_show_more: bool = False) -> list[RowDefinition]:
    """Return ordered row definitions for selected media type."""
    rows = ROW_REGISTRY.get(media_type, [])
    if include_show_more:
        return rows
    return [row for row in rows if not row.show_more]
