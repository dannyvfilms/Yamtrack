"""IMDB public non-commercial datasets provider.

Downloads title.ratings.tsv.gz and title.episode.tsv.gz from
https://datasets.imdbws.com/ (updated daily, free for non-commercial use)
and returns parsed data for bulk rating updates.
"""

import csv
import gzip
import io
import logging
import urllib.request

logger = logging.getLogger(__name__)

RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
EPISODE_URL = "https://datasets.imdbws.com/title.episode.tsv.gz"

_DOWNLOAD_TIMEOUT = 120


def download_ratings() -> dict[str, tuple[float, int]]:
    """Download and parse title.ratings.tsv.gz.

    Returns {tconst: (averageRating, numVotes)} for all titles.
    """
    logger.info("imdb_datasets: downloading ratings from %s", RATINGS_URL)
    with urllib.request.urlopen(RATINGS_URL, timeout=_DOWNLOAD_TIMEOUT) as resp:  # noqa: S310
        compressed = resp.read()

    ratings: dict[str, tuple[float, int]] = {}
    with gzip.open(io.BytesIO(compressed), "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            tconst = row.get("tconst", "").strip()
            avg = row.get("averageRating", "").strip()
            votes = row.get("numVotes", "").strip()
            if not tconst or avg == "\\N" or votes == "\\N":
                continue
            try:
                ratings[tconst] = (float(avg), int(votes))
            except (ValueError, TypeError):
                continue

    logger.info("imdb_datasets: loaded %d ratings", len(ratings))
    return ratings


def download_episode_map(parent_tconsts: set[str]) -> dict[str, dict[tuple[int, int], str]]:
    """Download and parse title.episode.tsv.gz, filtered to parent_tconsts.

    Streams the file to keep memory low (~85 MB uncompressed).
    Returns {parentTconst: {(seasonNumber, episodeNumber): episode_tconst}}.
    """
    if not parent_tconsts:
        return {}

    logger.info(
        "imdb_datasets: downloading episode map for %d shows from %s",
        len(parent_tconsts),
        EPISODE_URL,
    )
    result: dict[str, dict[tuple[int, int], str]] = {}

    with urllib.request.urlopen(EPISODE_URL, timeout=_DOWNLOAD_TIMEOUT) as resp:  # noqa: S310
        with gzip.open(resp, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                parent = row.get("parentTconst", "").strip()
                if parent not in parent_tconsts:
                    continue
                tconst = row.get("tconst", "").strip()
                sn_raw = row.get("seasonNumber", "").strip()
                en_raw = row.get("episodeNumber", "").strip()
                if not tconst or sn_raw == "\\N" or en_raw == "\\N":
                    continue
                try:
                    sn, en = int(sn_raw), int(en_raw)
                except (ValueError, TypeError):
                    continue
                result.setdefault(parent, {})[(sn, en)] = tconst

    logger.info(
        "imdb_datasets: episode map built for %d shows",
        len(result),
    )
    return result
