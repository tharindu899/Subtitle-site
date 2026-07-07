from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

import httpx

from .config import settings

TMDB_BASE = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w500"
BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"
STILL_BASE = "https://image.tmdb.org/t/p/w780"
PROFILE_BASE = "https://image.tmdb.org/t/p/w185"


class TMDBError(RuntimeError):
    pass


def _normalise_title(value: str) -> str:
    """Normalise a title for matching without changing the TMDB display title."""
    text = (value or "").lower()
    text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [word for word in text.split() if word not in {"the", "a", "an"}]
    return " ".join(words)


def _title_year(value: str) -> str:
    match = re.search(r"\b((?:19|20)\d{2})\b", value or "")
    return match.group(1) if match else ""


def match_score(item: dict[str, Any], query: str, expected_kind: str | None = None) -> float:
    """Rank exact title/type matches above loose keyword matches.

    A subtitle named ``Loki S01E01`` must put the TV series Loki ahead of a
    one-off movie that merely contains the word "Loki".
    """
    query_norm = _normalise_title(query)
    result_norm = _normalise_title(str(item.get("name") or ""))
    if not query_norm or not result_norm:
        return -10_000.0

    query_tokens = set(query_norm.split())
    result_tokens = set(result_norm.split())
    overlap = len(query_tokens & result_tokens)
    coverage = overlap / max(len(query_tokens), 1)
    precision = overlap / max(len(result_tokens), 1)
    ratio = SequenceMatcher(None, query_norm, result_norm).ratio()

    score = coverage * 380 + precision * 120 + ratio * 180
    if result_norm == query_norm:
        score += 1_000
    elif result_norm.startswith(query_norm):
        score += 520
    elif query_norm in result_norm:
        score += 170

    query_year = _title_year(query)
    result_year = str(item.get("year") or "")
    if query_year:
        if result_year == query_year:
            score += 120
        elif result_year:
            score -= min(abs(int(query_year) - int(result_year)), 20) * 3

    if expected_kind in {"movie", "tv"}:
        score += 340 if item.get("type") == expected_kind else -520

    # Popularity helps tie-break real matches only; it never beats an exact title.
    score += min(float(item.get("popularity") or 0), 120) * 0.08
    return score


def rank_results(results: list[dict[str, Any]], query: str, expected_kind: str | None = None) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for item in results:
        result = dict(item)
        result["match_score"] = round(match_score(result, query, expected_kind), 2)
        ranked.append(result)
    return sorted(ranked, key=lambda item: (float(item.get("match_score") or 0), float(item.get("popularity") or 0), float(item.get("rating") or 0)), reverse=True)


def is_confident_match(result: dict[str, Any] | None, query: str, expected_kind: str | None = None) -> bool:
    if not result:
        return False
    # Exact/near-exact titles are safe for automatic channel imports. Anything
    # more ambiguous remains in review instead of being published under a wrong title.
    score = float(result.get("match_score") or match_score(result, query, expected_kind))
    return score >= 700


async def request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not settings.tmdb_api_key:
        raise TMDBError("TMDB_API is not configured.")
    query = {"api_key": settings.tmdb_api_key, "language": "en-US"}
    query.update(params or {})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(18.0, connect=8.0)) as client:
            response = await client.get(f"{TMDB_BASE}{path}", params=query)
    except httpx.HTTPError as error:
        raise TMDBError("TMDB could not be reached.") from error
    try:
        payload = response.json()
    except ValueError as error:
        raise TMDBError("TMDB returned an unreadable response.") from error
    if not response.is_success:
        message = payload.get("status_message") if isinstance(payload, dict) else ""
        if response.status_code in {401, 403}:
            message = "TMDB_API was rejected. Check the TMDB API key."
        raise TMDBError(message or f"TMDB request failed (HTTP {response.status_code}).")
    return payload


def poster(path: str | None) -> str:
    return f"{POSTER_BASE}{path}" if path else ""


def backdrop(path: str | None) -> str:
    """Return a wide TMDB backdrop for full-screen title pages."""
    return f"{BACKDROP_BASE}{path}" if path else ""


def still(path: str | None) -> str:
    """Return a 16:9 episode still without using the smaller poster size."""
    return f"{STILL_BASE}{path}" if path else ""


def profile(path: str | None) -> str:
    return f"{PROFILE_BASE}{path}" if path else ""


def compact_result(item: dict[str, Any]) -> dict[str, Any] | None:
    kind = item.get("media_type")
    if kind not in {"movie", "tv"}:
        return None
    date = item.get("release_date") if kind == "movie" else item.get("first_air_date")
    name = item.get("title") if kind == "movie" else item.get("name")
    return {
        "id": int(item["id"]),
        "type": kind,
        "name": name or "Untitled",
        "year": str(date or "")[:4],
        "poster_url": poster(item.get("poster_path")),
        "rating": round(float(item.get("vote_average") or 0), 1),
        "popularity": float(item.get("popularity") or 0),
        "overview": item.get("overview") or "",
    }


async def search(query: str, kind: str = "all", expected_kind: str | None = None) -> list[dict[str, Any]]:
    """Search TMDB for a title.

    Filenames often carry the release year in the same string as the title
    (``Michael 2026``). Sending that whole phrase as TMDB's free-text
    ``query`` param can suppress matches, because the actual TMDB title
    rarely contains the year as literal text. Instead, the year is pulled
    out and sent through TMDB's dedicated year filter, and the search is
    retried without any year filter if that first attempt comes up empty
    (the filename's year and TMDB's recorded release year can disagree).
    """
    original_query = query.strip()
    if len(original_query) < 2:
        return []
    year = _title_year(original_query)
    text_query = re.sub(r"\b(?:19|20)\d{2}\b", " ", original_query)
    text_query = re.sub(r"\s+", " ", text_query).strip() or original_query
    endpoint = f"/search/{kind}" if kind in {"movie", "tv"} else "/search/multi"

    async def run(with_year: bool) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": text_query, "include_adult": "false", "page": 1}
        if with_year and year:
            if kind == "movie":
                params["primary_release_year"] = year
            elif kind == "tv":
                params["first_air_date_year"] = year
        payload = await request(endpoint, params)
        found: list[dict[str, Any]] = []
        for item in payload.get("results", []):
            if kind in {"movie", "tv"}:
                item = {**item, "media_type": kind}
            result = compact_result(item)
            if result:
                found.append(result)
        return found

    results = await run(with_year=True)
    ranked = rank_results(results, original_query, expected_kind)
    if not ranked and year:
        # The year filter found nothing — try again without it.
        results = await run(with_year=False)
        ranked = rank_results(results, original_query, expected_kind)
    return ranked[:16]


async def episode_details(tmdb_id: int, season: int, episode: int) -> dict[str, Any]:
    """Load one TV episode from TMDB for an episode-specific subtitle page."""
    if tmdb_id <= 0 or season <= 0 or episode <= 0:
        raise TMDBError("A valid series, season and episode are required.")
    payload = await request(f"/tv/{tmdb_id}/season/{season}/episode/{episode}")
    return {
        "season": int(payload.get("season_number") or season),
        "episode": int(payload.get("episode_number") or episode),
        "name": payload.get("name") or f"Episode {episode}",
        "overview": payload.get("overview") or "",
        "still_url": still(payload.get("still_path")),
        "air_date": payload.get("air_date") or "",
        "rating": round(float(payload.get("vote_average") or 0), 1),
        "runtime": int(payload.get("runtime") or 0),
    }


async def details(kind: str, tmdb_id: int) -> dict[str, Any]:
    if kind not in {"movie", "tv"}:
        raise TMDBError("Choose a movie or series from TMDB.")
    payload = await request(f"/{kind}/{tmdb_id}", {"append_to_response": "credits"})
    date = payload.get("release_date") if kind == "movie" else payload.get("first_air_date")
    credits = payload.get("credits") or {}
    cast = [
        {
            "name": person.get("name"),
            "character": person.get("character") or "",
            "photo_url": profile(person.get("profile_path")),
        }
        for person in credits.get("cast", [])[:16]
        if person.get("name")
    ]
    return {
        "tmdb_id": int(payload["id"]),
        "tmdb_type": kind,
        "name": payload.get("title") if kind == "movie" else payload.get("name") or "Untitled",
        "media_label": "Movie" if kind == "movie" else "Series",
        "release_year": int(str(date)[:4]) if str(date)[:4].isdigit() else None,
        "poster_url": poster(payload.get("poster_path")),
        "backdrop_url": backdrop(payload.get("backdrop_path")),
        "rating": round(float(payload.get("vote_average") or 0), 1),
        "overview": payload.get("overview") or "",
        "cast": cast,
    }
