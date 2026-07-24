"""TMDb client for Hebrew-first title search and IMDb id resolution.

Used only by the optional Hebrew group approval workflow (tg_admin_workflow.py).
Never required for normal Stremio streaming; every function degrades to an
empty/None result on missing config or network failure so the group workflow
can fall back to a manually entered IMDb id or the declared title.
"""

import logging
from typing import Optional

import httpx

from config import Config
from utils import BoundedTTLCache

logger = logging.getLogger("tmdb_client")

TMDB_BASE = "https://api.themoviedb.org/3"
_REQUEST_TIMEOUT = 8.0

_search_cache = BoundedTTLCache(maxsize=500, ttl=6 * 3600)
_external_id_cache = BoundedTTLCache(maxsize=1000, ttl=24 * 3600)


def is_configured() -> bool:
    return Config.tmdb_enabled()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {Config.TMDB_BEARER_TOKEN}",
        "accept": "application/json",
    }


class TmdbCandidate:
    __slots__ = ("tmdb_id", "media_type", "title", "original_title", "year", "poster_path", "overview")

    def __init__(self, tmdb_id, media_type, title, original_title, year, poster_path, overview):
        self.tmdb_id = tmdb_id
        self.media_type = media_type  # "movie" or "tv"
        self.title = title
        self.original_title = original_title
        self.year = year
        self.poster_path = poster_path
        self.overview = overview

    def stremio_type(self) -> str:
        return "series" if self.media_type == "tv" else "movie"

    def to_dict(self) -> dict:
        return {
            "tmdb_id": self.tmdb_id,
            "media_type": self.media_type,
            "title": self.title,
            "original_title": self.original_title,
            "year": self.year,
            "poster_path": self.poster_path,
            "overview": self.overview,
        }


async def search(query: str, media_type: str = None, language: str = "he-IL", limit: int = 15) -> list:
    """Search TMDb for a title, Hebrew-localized by default.

    media_type: None (multi search), "movie", or "tv".
    Returns a list of TmdbCandidate, newer releases first, capped at `limit`
    (default 15 for the Telegram pick-list).
    """
    if not is_configured() or not query or not query.strip():
        return []

    limit = max(1, min(int(limit or 15), 15))
    cache_key = f"{media_type or 'multi'}:{language}:{limit}:{query.strip().lower()}"
    cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached

    endpoint = {
        "movie": "/search/movie",
        "tv": "/search/tv",
    }.get(media_type, "/search/multi")

    params = {"query": query.strip(), "language": language, "include_adult": "false"}

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(f"{TMDB_BASE}{endpoint}", params=params, headers=_headers())
            if resp.status_code != 200:
                logger.warning(f"TMDb search failed with status {resp.status_code}")
                return []
            data = resp.json()
    except Exception as e:
        logger.error(f"TMDb search request failed: {e}")
        return []

    candidates = []
    for item in data.get("results", []):
        item_media_type = media_type or item.get("media_type")
        if item_media_type not in ("movie", "tv"):
            continue

        is_movie = item_media_type == "movie"
        title = item.get("title") if is_movie else item.get("name")
        original_title = item.get("original_title") if is_movie else item.get("original_name")
        date_str = item.get("release_date") if is_movie else item.get("first_air_date")
        year = None
        if date_str:
            try:
                year = int(date_str.split("-")[0])
            except Exception:
                pass

        if not title:
            continue

        candidates.append(
            TmdbCandidate(
                tmdb_id=item.get("id"),
                media_type=item_media_type,
                title=title,
                original_title=original_title or title,
                year=year,
                poster_path=item.get("poster_path"),
                overview=item.get("overview") or "",
            )
        )

    # When several titles match, prefer the later release (newer year).
    # Entries without a year sort last.
    candidates.sort(key=lambda c: (c.year is not None, c.year or 0), reverse=True)
    candidates = candidates[:limit]

    _search_cache.set(cache_key, candidates)
    return candidates


async def get_external_ids(tmdb_id: int, media_type: str) -> Optional[dict]:
    """Fetch external ids (notably imdb_id) for a TMDb movie/tv entry."""
    if not is_configured() or not tmdb_id or media_type not in ("movie", "tv"):
        return None

    cache_key = f"{media_type}:{tmdb_id}"
    cached = _external_id_cache.get(cache_key)
    if cached is not None:
        return cached

    path = f"/{media_type}/{tmdb_id}/external_ids"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(f"{TMDB_BASE}{path}", headers=_headers())
            if resp.status_code != 200:
                logger.warning(f"TMDb external_ids failed with status {resp.status_code}")
                return None
            data = resp.json()
    except Exception as e:
        logger.error(f"TMDb external_ids request failed: {e}")
        return None

    result = {"imdb_id": data.get("imdb_id") or None}
    _external_id_cache.set(cache_key, result)
    return result


async def resolve_candidate_imdb(candidate: TmdbCandidate) -> Optional[str]:
    """Convenience helper: TmdbCandidate -> IMDb tt id, or None."""
    ids = await get_external_ids(candidate.tmdb_id, candidate.media_type)
    if ids and ids.get("imdb_id"):
        return ids["imdb_id"]
    return None
