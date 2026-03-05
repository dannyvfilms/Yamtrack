"""Discover row registry."""

from __future__ import annotations

from app.models import MediaTypes

from app.discover.schemas import RowDefinition

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
    ALL_MEDIA_KEY: [
        RowDefinition(
            key="continue_all",
            title="Continue Across All Media",
            mission="Next Action",
            why="Pick up where you left off",
            source="local",
            min_items=1,
            allow_tracked=True,
        ),
        RowDefinition(
            key="trending_all",
            title="Trending Across All Media",
            mission="Cultural Moment",
            why="What people are talking about now",
            source="hybrid",
        ),
        RowDefinition(
            key="top_picks_all",
            title="Top Picks For You",
            mission="Personal Taste Match",
            why="Best fit based on your recent activity",
            source="hybrid",
        ),
    ],
    MediaTypes.MOVIE.value: [
        RowDefinition("trending_right_now", "Trending Right Now", "Cultural Moment", "What everyone has been watching this week.", "trakt"),
        RowDefinition("all_time_greats_unseen", "All-Time Greats You Haven't Seen", "Canon", "Must-watch classics still missing", "trakt"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming releases to watchlist", "trakt"),
        RowDefinition("top_picks_for_you", "Top Picks For You", "Personal Taste Match", "New-to-you movies tailored to your taste.", "local", allow_tracked=True),
        RowDefinition("comfort_rewatches", "Comfort Rewatches", "Comfort", "Favorites you loved, ready for a revisit.", "local", allow_tracked=True),
    ],
    MediaTypes.TV.value: [
        RowDefinition("next_episode", "Next Episode", "Next Action", "Your most likely next episode", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending_tv", "Trending TV", "Cultural Moment", "The shows everyone is discussing", "tmdb"),
        RowDefinition("new_returning_seasons", "New & Returning Seasons", "Cultural Moment", "Currently airing titles to follow", "tmdb"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Premieres and upcoming seasons", "tmdb"),
        RowDefinition("all_time_great_tv", "All-Time Great TV", "Canon", "Landmark series you still haven't finished", "tmdb"),
        RowDefinition("because_you_liked", "Because You Liked", "Personal Taste Match", "Closely related to your favorites", "hybrid"),
        RowDefinition("backlog_ranked", "Your Backlog, Ranked", "Next Action", "Planning list ordered by taste fit", "local"),
        RowDefinition("comfort_binge", "Comfort Binge", "Comfort", "Past favorites ready for a rewatch", "local", show_more=True, min_items=1, allow_tracked=True),
        RowDefinition("genre_spotlight", "Genre Spotlight", "Personal Taste Match", "A focused lane you already enjoy", "hybrid", show_more=True),
        RowDefinition("short_runs", "Short Runs", "Next Action", "Lower commitment picks from backlog", "local", show_more=True),
    ],
    MediaTypes.ANIME.value: [
        RowDefinition("continue", "Continue", "Next Action", "Keep momentum on active titles", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending", "Trending Anime", "Cultural Moment", "What anime fans are following now", "provider"),
        RowDefinition("new_seasons", "New Seasons", "Cultural Moment", "Current seasonal highlights", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming anime to keep on radar", "provider"),
        RowDefinition("all_time_greats", "All-Time Greats You Haven't Seen", "Canon", "Classic picks still missing", "provider"),
        RowDefinition("because_you_watched", "Because You Watched", "Personal Taste Match", "Related titles based on your history", "hybrid"),
        RowDefinition("backlog_ranked", "Your Backlog, Ranked", "Next Action", "Plan-to-watch ordered by fit", "local"),
    ],
    MediaTypes.MUSIC.value: [
        RowDefinition("continue", "Keep Listening", "Next Action", "Albums and artists in active rotation", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending", "Trending Now", "Cultural Moment", "What listeners are spinning now", "provider"),
        RowDefinition("new_releases", "New Releases You'd Like", "Cultural Moment", "Fresh drops matching your taste", "provider"),
        RowDefinition("all_time_greats", "All-Time Greats You Haven't Heard", "Canon", "Essential listens still missing", "provider"),
        RowDefinition("because_you_played", "Because You Played", "Personal Taste Match", "Related artists and albums", "hybrid"),
        RowDefinition("backlog_ranked", "Your Queue, Ranked", "Next Action", "Saved listens ordered by fit", "local"),
        RowDefinition("comfort", "Comfort Spins", "Comfort", "Favorite repeats after a long gap", "local", min_items=1, allow_tracked=True),
    ],
    MediaTypes.PODCAST.value: [
        RowDefinition("continue", "Continue Episode", "Next Action", "Resume unfinished episodes", "local", min_items=1, allow_tracked=True),
        RowDefinition("new_episodes", "New Episodes From Your Shows", "Cultural Moment", "Fresh drops from subscriptions", "provider"),
        RowDefinition("trending", "Trending Podcasts", "Cultural Moment", "Popular shows right now", "provider"),
        RowDefinition("top_untried", "Top Shows You Haven't Tried", "Canon", "Highly rated shows outside your queue", "provider"),
        RowDefinition("because_you_listen", "Because You Listen To", "Personal Taste Match", "Shows similar to your favorites", "hybrid"),
        RowDefinition("backlog_ranked", "Your Queue", "Next Action", "Queued episodes ordered by fit", "local"),
        RowDefinition("comfort", "Comfort Relisten", "Comfort", "Go-to episodes worth revisiting", "local", min_items=1, allow_tracked=True),
    ],
    MediaTypes.BOOK.value: [
        RowDefinition("continue", "Currently Reading", "Next Action", "Pick up your active reads", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending", "Trending This Month", "Cultural Moment", "Popular books this month", "provider"),
        RowDefinition("new_releases", "New Releases", "Cultural Moment", "Fresh publications", "provider"),
        RowDefinition("all_time_greats", "All-Time Greats You Haven't Read", "Canon", "Classics still on your shelf", "provider"),
        RowDefinition("because_you_read", "Because You Read", "Personal Taste Match", "Similar books based on your reads", "hybrid"),
        RowDefinition("backlog_ranked", "Your Reading Backlog, Ranked", "Next Action", "TBR list ordered by fit", "local"),
        RowDefinition("comfort", "Comfort Reads", "Comfort", "Favorites to revisit", "local", min_items=1, allow_tracked=True),
    ],
    MediaTypes.COMIC.value: [
        RowDefinition("continue", "Currently Reading", "Next Action", "Continue ongoing runs", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending", "Trending This Month", "Cultural Moment", "Popular issues and runs", "provider"),
        RowDefinition("new_releases", "New Releases", "Cultural Moment", "Fresh issues and volumes", "provider"),
        RowDefinition("all_time_greats", "All-Time Greats You Haven't Read", "Canon", "Must-read runs still missing", "provider"),
        RowDefinition("because_you_read", "Because You Read", "Personal Taste Match", "Similar creators and series", "hybrid"),
        RowDefinition("backlog_ranked", "Your Reading Backlog, Ranked", "Next Action", "Queue ordered by fit", "local"),
        RowDefinition("comfort", "Comfort Reads", "Comfort", "Old favorites worth revisiting", "local", min_items=1, allow_tracked=True),
    ],
    MediaTypes.MANGA.value: [
        RowDefinition("continue", "Currently Reading", "Next Action", "Continue ongoing series", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending", "Trending This Month", "Cultural Moment", "Series readers are following now", "provider"),
        RowDefinition("new_releases", "New Releases", "Cultural Moment", "Latest chapters and volumes", "provider"),
        RowDefinition("all_time_greats", "All-Time Greats You Haven't Read", "Canon", "Classic manga still in backlog", "provider"),
        RowDefinition("because_you_read", "Because You Read", "Personal Taste Match", "Related manga and creators", "hybrid"),
        RowDefinition("backlog_ranked", "Your Reading Backlog, Ranked", "Next Action", "Plan-to-read ordered by fit", "local"),
        RowDefinition("comfort", "Comfort Reads", "Comfort", "Series to revisit", "local", min_items=1, allow_tracked=True),
    ],
    MediaTypes.GAME.value: [
        RowDefinition("continue", "Continue Playing", "Next Action", "Resume active games", "local", min_items=1, allow_tracked=True),
        RowDefinition("trending", "Trending Games", "Cultural Moment", "Games drawing attention now", "provider"),
        RowDefinition("new_releases", "New Releases", "Cultural Moment", "Recently launched titles", "provider"),
        RowDefinition("coming_soon", "Coming Soon", "Anticipation", "Upcoming launches", "provider"),
        RowDefinition("all_time_greats", "All-Time Best You Haven't Played", "Canon", "Essential games still missing", "provider"),
        RowDefinition("because_you_played", "Because You Played", "Personal Taste Match", "Similar picks based on your history", "hybrid"),
        RowDefinition("backlog_ranked", "Your Backlog, Ranked", "Next Action", "Play-next queue ordered by fit", "local"),
        RowDefinition("comfort", "Comfort Replay", "Comfort", "Past favorites ready to replay", "local", show_more=True, min_items=1, allow_tracked=True),
        RowDefinition("quick_plays", "Quick Plays", "Next Action", "Lower-commitment games", "hybrid", show_more=True),
    ],
    MediaTypes.BOARDGAME.value: [
        RowDefinition("backlog", "Your Shelf of Shame", "Next Action", "Unplayed games waiting for table time", "local", min_items=1, allow_tracked=True),
        RowDefinition("hotness", "BGG Hotness", "Cultural Moment", "What tabletop players are buzzing about", "provider"),
        RowDefinition("new_releases", "New Releases", "Cultural Moment", "Recently published games", "provider"),
        RowDefinition("top_100", "BGG Top 100 You Haven't Played", "Canon", "Foundational games still missing", "provider"),
        RowDefinition("similar_favorites", "Similar to Your Favorites", "Personal Taste Match", "Mechanically similar options", "hybrid"),
        RowDefinition("great_tonight", "Great For Tonight", "Next Action", "Fits your likely session", "hybrid"),
        RowDefinition("comfort", "Comfort Favorites", "Comfort", "Reliable favorites to replay", "local", min_items=1, allow_tracked=True),
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
