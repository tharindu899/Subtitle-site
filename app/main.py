from __future__ import annotations

import hashlib
import html
import logging
from html.parser import HTMLParser
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pymongo.errors import DuplicateKeyError
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, settings
from .database import close_database, get_db, initialise_database, new_id, utcnow
from .parsing import (
    ALLOWED_EXTENSIONS,
    RESOLUTIONS,
    SOURCE_TYPES,
    caption_resolution,
    caption_source,
    caption_tmdb,
    caption_uploader,
    clean_username,
    parse_subtitle_name,
    valid_subtitle_filename,
)
from .telegram_service import TelegramService, TelegramStorageError
from .tmdb import (
    TMDBError,
    details as tmdb_details,
    episode_details as tmdb_episode_details,
    is_confident_match,
    search as tmdb_search,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("tharinduhub.subtitles")

APP_NAME = f"{settings.site_name} Subtitles"
STATIC_DIR = BASE_DIR / "app" / "static"
TEMPLATE_DIR = BASE_DIR / "app" / "templates"
BRAND_ICON = STATIC_DIR / "brand.svg"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def brand_icon_version() -> str:
    """Short hash so the browser refetches the SVG when branding changes."""
    if not BRAND_ICON.exists():
        return "0"
    return hashlib.md5(BRAND_ICON.read_bytes()).hexdigest()[:10]


def site_asset_version() -> str:
    """One cache-busting version for the hand-written Jinja CSS and JS."""
    digest = hashlib.md5()
    for path in (STATIC_DIR / "css" / "site.css", STATIC_DIR / "js" / "site.js"):
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10] if digest.digest() else "0"


tg = TelegramService()


def format_size(value: int | None) -> str:
    size = int(value or 0)
    if not size:
        return "—"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.0f} KB"


def format_count(value: int | None) -> str:
    number = int(value or 0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def format_date(value: Any) -> str:
    try:
        return value.strftime("%d %b %Y") if value else "—"
    except AttributeError:
        return str(value)[:10]


EPISODE_SLUG = re.compile(r"^s(?P<season>\d{1,3})e(?P<episode>\d{1,4})$", re.IGNORECASE)
MAX_EPISODE_SPAN = 32
TMDB_IMAGE_PATH = re.compile(r"^(https://image\.tmdb\.org/t/p/)(?:w\d+|original)(/)", re.IGNORECASE)


def tmdb_image_size(value: Any, size: str) -> str:
    """Use the right TMDB size for the visual slot without changing other images."""
    source = str(value or "").strip()
    if not source:
        return ""
    return TMDB_IMAGE_PATH.sub(lambda match: f"{match.group(1)}{size}{match.group(2)}", source)


def public_base_url() -> str:
    """Return one clean public site address for Telegram URL buttons.

    A subtitle-channel button must always send a viewer to the real public site,
    never to a callback requiring team access. ``PUBLIC_BASE_URL`` is preferred;
    the older ``BASE_URL`` alias is accepted by ``Settings`` as a migration path.
    """
    raw = str(settings.public_base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        logger.warning("PUBLIC_BASE_URL is invalid; channel page buttons are disabled.")
        return ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


def channel_page_keyboard(title: dict[str, Any], season: Any = None, episode: Any = None) -> InlineKeyboardMarkup | None:
    """Create exactly one public button for a channel subtitle post.

    Movies open their title page, which contains the movie card and all existing
    subtitle files. Series episode releases open that exact episode page, which
    contains its TMDB details, optional note and matching files. This avoids the
    old duplicate pair of page/file buttons because the one destination already
    contains both parts.
    """
    base = public_base_url()
    media_type = str(title.get("tmdb_type") or "").lower()
    try:
        tmdb_id = int(title.get("tmdb_id") or 0)
    except (TypeError, ValueError):
        tmdb_id = 0
    if not base or media_type not in {"movie", "tv"} or tmdb_id <= 0:
        return None

    title_id = quote(f"{media_type}_{tmdb_id}", safe="")
    if media_type == "movie":
        destination = f"{base}/title/{title_id}"
        label = "🎬 Open movie & files"
    else:
        try:
            season_number = int(season or 0)
            episode_number = int(episode or 0)
        except (TypeError, ValueError):
            season_number = episode_number = 0
        if season_number > 0 and episode_number > 0:
            code = episode_slug(season_number, episode_number).upper()
            destination = f"{base}/title/{title_id}/{episode_slug(season_number, episode_number)}"
            label = f"📺 Open {code} & files"
        else:
            destination = f"{base}/title/{title_id}"
            label = "📺 Open series episodes"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=destination)]])


# A title-card note is authored by the subtitle team and rendered on a public
# page. Keep the useful editorial HTML while stripping scripts, embedded pages,
# styles and unsafe URL schemes before it ever reaches the website.
RICH_NOTE_TAGS = frozenset({
    "a", "b", "blockquote", "br", "details", "div", "em", "figcaption",
    "figure", "h2", "h3", "h4", "hr", "i", "img", "li", "mark", "ol",
    "p", "span", "strong", "summary", "table", "tbody", "td", "th", "thead",
    "tr", "u", "ul",
})
RICH_NOTE_VOID_TAGS = frozenset({"br", "hr", "img"})
RICH_NOTE_BLOCKED_TAGS = frozenset({
    "applet", "base", "button", "embed", "form", "frame", "frameset", "iframe",
    "input", "link", "math", "meta", "noscript", "object", "script", "select",
    "style", "svg", "template", "textarea", "video",
})


def safe_rich_note_url(value: str | None) -> str:
    """Return a safe public link/image URL or an empty string.

    Rich notes intentionally accept only same-site paths and HTTPS resources.
    This keeps a pasted image or source link useful without allowing javascript,
    data, file or protocol-relative URLs onto a public page.
    """
    raw = html.unescape(str(value or "").strip())
    if not raw or len(raw) > 2048 or any(character in raw for character in "\r\n\t"):
        return ""
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() == "https" and parsed.netloc:
        return raw
    return ""


def safe_rich_note_dimension(value: str | None) -> str:
    try:
        number = int(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    return str(max(1, min(number, 2400)))


class RichNoteSanitizer(HTMLParser):
    """Small allow-list HTML sanitizer for editorial movie and series notes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_depth = 0
        self.used_markup = False
        self.image_count = 0
        self.text_count = 0

    def _append(self, value: str) -> None:
        self.parts.append(value)

    def _attrs_for(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        values = {str(key or "").lower(): str(value or "") for key, value in attrs}
        if tag == "a":
            href = safe_rich_note_url(values.get("href"))
            if not href:
                return ""
            title = html.escape(values.get("title", "")[:180], quote=True)
            suffix = f' title="{title}"' if title else ""
            return f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"{suffix}'
        if tag == "img":
            if self.image_count >= 12:
                return None
            src = safe_rich_note_url(values.get("src"))
            if not src:
                return None
            self.image_count += 1
            alt = html.escape(values.get("alt", "")[:240], quote=True)
            title = html.escape(values.get("title", "")[:180], quote=True)
            width = safe_rich_note_dimension(values.get("width"))
            height = safe_rich_note_dimension(values.get("height"))
            extras = f' alt="{alt}" loading="lazy" decoding="async"'
            if title:
                extras += f' title="{title}"'
            if width:
                extras += f' width="{width}"'
            if height:
                extras += f' height="{height}"'
            return f' src="{html.escape(src, quote=True)}"{extras}'
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag in RICH_NOTE_BLOCKED_TAGS:
                self.blocked_depth += 1
            return
        if tag in RICH_NOTE_BLOCKED_TAGS:
            self.blocked_depth = 1
            return
        if tag not in RICH_NOTE_TAGS:
            return
        rendered_attrs = self._attrs_for(tag, attrs)
        if rendered_attrs is None:
            return
        self.used_markup = True
        self._append(f"<{tag}{rendered_attrs}>")
        if tag not in RICH_NOTE_VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in RICH_NOTE_VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag in RICH_NOTE_BLOCKED_TAGS:
                self.blocked_depth -= 1
            return
        if tag not in RICH_NOTE_TAGS or tag in RICH_NOTE_VOID_TAGS or tag not in self.open_tags:
            return
        while self.open_tags:
            current = self.open_tags.pop()
            self._append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.blocked_depth or not data or self.text_count >= 10_000:
            return
        safe_data = data[: 10_000 - self.text_count]
        self.text_count += len(safe_data)
        self._append(html.escape(safe_data))

    def rendered(self) -> str:
        while self.open_tags:
            self._append(f"</{self.open_tags.pop()}>")
        value = "".join(self.parts).strip()
        if not value:
            return ""
        if not self.used_markup:
            return f"<p>{value.replace(chr(13), '').replace(chr(10), '<br>')}</p>"
        return value


def title_page_note_html(raw: str | None) -> str:
    source = str(raw or "").strip()
    if not source or source.lower() in {"-", "skip"}:
        return ""
    parser = RichNoteSanitizer()
    try:
        parser.feed(source[:12_000])
        parser.close()
    except Exception:
        return ""
    return parser.rendered()


def episode_slug(season: int, episode: int) -> str:
    return f"s{int(season):02d}e{int(episode):02d}"


def episode_page_id(title_id: str, season: int, episode: int) -> str:
    return f"episode:{title_id}:{int(season)}:{int(episode)}"


def subtitle_episode_targets(subtitle: dict[str, Any]) -> list[tuple[int, int]]:
    """Return every discrete page covered by one subtitle release.

    Multi-episode subtitle files such as S01E03-E04 appear on both episode
    pages. A defensive span limit prevents malformed filenames from creating
    thousands of virtual pages.
    """
    try:
        season = int(subtitle.get("season") or 0)
        start = int(subtitle.get("episode") or 0)
    except (TypeError, ValueError):
        return []
    if season <= 0 or start <= 0:
        return []
    try:
        end = int(subtitle.get("episode_end") or start)
    except (TypeError, ValueError):
        end = start
    if end < start or end - start >= MAX_EPISODE_SPAN:
        end = start
    return [(season, number) for number in range(start, end + 1)]


def subtitle_matches_episode(subtitle: dict[str, Any], season: int, episode: int) -> bool:
    return (int(season), int(episode)) in subtitle_episode_targets(subtitle)


def title_episode_groups(subtitles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make a stable episode index for the TV-title page without TMDB calls."""
    groups: dict[tuple[int, int], dict[str, Any]] = {}
    for subtitle in subtitles:
        for season, episode in subtitle_episode_targets(subtitle):
            key = (season, episode)
            item = groups.setdefault(
                key,
                {
                    "season": season,
                    "episode": episode,
                    "slug": episode_slug(season, episode),
                    "fileCount": 0,
                    "latestAt": None,
                },
            )
            item["fileCount"] += 1
            updated_at = subtitle.get("updated_at") or subtitle.get("created_at")
            if updated_at and (item["latestAt"] is None or updated_at > item["latestAt"]):
                item["latestAt"] = updated_at
    return [
        {**item, "latestAt": iso(item.get("latestAt"))}
        for _, item in sorted(groups.items(), key=lambda pair: pair[0])
    ]


def episode_page_note_html(raw: str | None) -> str:
    """Sanitise the optional rich note attached to one TV episode page.

    Episode notes intentionally use the same safe HTML allow-list as movie
    notes. A team member can add a long Sinhala explanation, images, lists,
    links and detail boxes without exposing unsafe markup on the public site.
    """
    return title_page_note_html(raw)


async def episode_page_data(title: dict[str, Any], season: int, episode: int) -> dict[str, Any]:
    """Return cached TMDB episode metadata and the optional team page note.

    The first episode visit pulls its own synopsis from TMDB, then stores it
    in MongoDB. A temporary TMDB outage never blocks subtitle downloads.
    """
    database = get_db()
    page_id = episode_page_id(str(title["_id"]), season, episode)
    cached = await database.episode_pages.find_one({"_id": page_id}) or {}
    last_attempt = cached.get("tmdb_attempted_at") or cached.get("tmdb_checked_at")
    lookup_ready = cached.get("tmdb_status") == "ready"
    refresh = not last_attempt
    if last_attempt:
        try:
            refresh_after = timedelta(days=28) if lookup_ready else timedelta(hours=6)
            refresh = utcnow() - last_attempt > refresh_after
        except TypeError:
            refresh = True

    if refresh:
        tmdb_record: dict[str, Any] = {}
        lookup_succeeded = False
        try:
            tmdb_record = await tmdb_episode_details(int(title.get("tmdb_id") or 0), season, episode)
            lookup_succeeded = True
        except (TMDBError, ValueError, TypeError) as error:
            logger.info("Episode TMDB lookup skipped for %s S%02dE%02d: %s", title.get("_id"), season, episode, error)
        now = utcnow()
        await database.episode_pages.update_one(
            {"_id": page_id},
            {
                "$set": {
                    "title_id": title["_id"],
                    "season": int(season),
                    "episode": int(episode),
                    "tmdb_id": title.get("tmdb_id"),
                    "tmdb_name": tmdb_record.get("name") or cached.get("tmdb_name") or f"Episode {episode}",
                    "tmdb_overview": tmdb_record.get("overview") or cached.get("tmdb_overview") or "",
                    "still_url": tmdb_record.get("still_url") or cached.get("still_url") or "",
                    "air_date": tmdb_record.get("air_date") or cached.get("air_date") or "",
                    "rating": tmdb_record.get("rating") if lookup_succeeded else float(cached.get("rating") or 0),
                    "runtime": tmdb_record.get("runtime") if lookup_succeeded else int(cached.get("runtime") or 0),
                    "tmdb_attempted_at": now,
                    "tmdb_status": "ready" if lookup_succeeded else "retry",
                    "tmdb_checked_at": now if lookup_succeeded else cached.get("tmdb_checked_at"),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now, "manual_note_html": ""},
            },
            upsert=True,
        )
        cached = await database.episode_pages.find_one({"_id": page_id}) or cached

    return {
        "season": int(cached.get("season") or season),
        "number": int(cached.get("episode") or episode),
        "code": episode_slug(season, episode).upper(),
        "slug": episode_slug(season, episode),
        "name": cached.get("tmdb_name") or f"Episode {episode}",
        "overview": cached.get("tmdb_overview") or "",
        "still": tmdb_image_size(cached.get("still_url"), "w780"),
        "airDate": cached.get("air_date") or "",
        "rating": float(cached.get("rating") or 0),
        "runtime": int(cached.get("runtime") or 0),
        "manualNoteHtml": episode_page_note_html(cached.get("manual_note_html") or cached.get("manual_note")),
    }


async def save_episode_page_note(
    title: dict[str, Any], season: int, episode: int, note: str, member: dict[str, Any]
) -> None:
    """Persist the optional note set from the Telegram subtitle draft."""
    now = utcnow()
    await get_db().episode_pages.update_one(
        {"_id": episode_page_id(str(title["_id"]), season, episode)},
        {
            "$set": {
                "title_id": title["_id"],
                "season": int(season),
                "episode": int(episode),
                "tmdb_id": title.get("tmdb_id"),
                "manual_note_html": episode_page_note_html(note),
                "manual_note_updated_at": now,
                "manual_note_by": member.get("_id"),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now, "tmdb_checked_at": None},
        },
        upsert=True,
    )


async def save_draft_episode_note(draft: dict[str, Any], title: dict[str, Any], member: dict[str, Any]) -> None:
    if not draft.get("episode_note_touched"):
        return
    if title.get("tmdb_type") != "tv":
        return
    for season, episode in subtitle_episode_targets(draft):
        await save_episode_page_note(title, season, episode, str(draft.get("episode_note") or ""), member)


async def save_draft_title_note(draft: dict[str, Any], title: dict[str, Any], member: dict[str, Any]) -> None:
    """Save the optional rich note shared by the public movie/series title page."""
    if not draft.get("title_note_touched"):
        return
    if title.get("tmdb_type") != "movie":
        return
    now = utcnow()
    await get_db().titles.update_one(
        {"_id": title["_id"]},
        {
            "$set": {
                "page_note_html": title_page_note_html(draft.get("title_note_html")),
                "page_note_updated_at": now,
                "page_note_by": member.get("_id"),
                "updated_at": now,
            }
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public web helpers
# ─────────────────────────────────────────────────────────────────────────────
def visitor_id(request: Request) -> str:
    value = request.session.get("viewer_id")
    if not value:
        value = new_id("viewer_")
        request.session["viewer_id"] = value
    return value


def iso(value: Any) -> str | None:
    try:
        return value.isoformat() if value else None
    except AttributeError:
        return str(value) if value else None


async def active_ads() -> dict[str, str]:
    records = await get_db().settings.find({"type": "ad", "enabled": True}).to_list(length=10)
    return {record.get("slot", ""): record.get("code", "") for record in records if record.get("code")}


async def public_stats() -> dict[str, int]:
    database = get_db()
    visible = {"status": "published", "language": "Sinhala"}
    title_ids = await database.subtitles.distinct("title_id", visible)
    downloads = sum(
        row.get("download_count", 0)
        for row in await database.subtitles.find(visible, {"download_count": 1}).to_list(length=10000)
    )
    return {
        "titles": len(title_ids),
        "subtitles": await database.subtitles.count_documents(visible),
        "downloads": int(downloads),
    }


async def title_cards(query: str = "", media_type: str = "all", limit: int = 180) -> list[dict[str, Any]]:
    """Return only titles that currently have published Sinhala subtitle files.

    This is intentionally derived from the subtitle records instead of relying
    only on a cached counter, so older records cannot accidentally make a
    non-Sinhala or deleted title visible on the public site.
    """
    filters: dict[str, Any] = {"status": "published"}
    if media_type in {"movie", "tv"}:
        filters["tmdb_type"] = media_type
    if query.strip():
        filters["name"] = {"$regex": re.escape(query.strip()), "$options": "i"}
    pipeline = [
        {"$match": filters},
        {"$lookup": {
            "from": "subtitles",
            "let": {"title_id": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$title_id", "$$title_id"]},
                    {"$eq": ["$status", "published"]},
                    {"$eq": ["$language", "Sinhala"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "_sinhala_files",
        }},
        {"$set": {"_sinhala_subtitle_count": {"$ifNull": [{"$arrayElemAt": ["$_sinhala_files.count", 0]}, 0]}}},
        {"$match": {"_sinhala_subtitle_count": {"$gt": 0}}},
        {"$sort": {"updated_at": -1}},
        {"$limit": int(limit)},
    ]
    return await get_db().titles.aggregate(pipeline).to_list(length=limit)


# ─────────────────────────────────────────────────────────────────────────────
# Database title / subtitle helpers
# ─────────────────────────────────────────────────────────────────────────────
async def ensure_title(tmdb_info: dict[str, Any] | None, fallback_name: str) -> dict[str, Any]:
    database = get_db()
    if tmdb_info:
        title_id = f"{tmdb_info['tmdb_type']}_{tmdb_info['tmdb_id']}"
        document = {
            "_id": title_id,
            "tmdb_id": tmdb_info["tmdb_id"],
            "tmdb_type": tmdb_info["tmdb_type"],
            "name": tmdb_info["name"],
            "media_label": tmdb_info["media_label"],
            "release_year": tmdb_info.get("release_year"),
            "poster_url": tmdb_info.get("poster_url", ""),
            "backdrop_url": tmdb_info.get("backdrop_url", ""),
            "rating": tmdb_info.get("rating", 0),
            "overview": tmdb_info.get("overview", ""),
            "cast": tmdb_info.get("cast", []),
            "status": "published",
            "updated_at": utcnow(),
        }
        await database.titles.update_one(
            {"_id": title_id},
            {"$set": document, "$setOnInsert": {"created_at": utcnow(), "subtitle_count": 0, "download_count": 0}},
            upsert=True,
        )
        return await database.titles.find_one({"_id": title_id})

    title_id = new_id("manual_")
    document = {
        "_id": title_id,
        "tmdb_id": None,
        "tmdb_type": "unknown",
        "name": fallback_name[:160] or "Unmatched subtitle",
        "media_label": "Needs review",
        "release_year": None,
        "poster_url": "",
        "backdrop_url": "",
        "rating": 0,
        "overview": "The file needs a TMDB match from an editor or owner.",
        "cast": [],
        "status": "review",
        "subtitle_count": 0,
        "download_count": 0,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    await database.titles.insert_one(document)
    return document


async def refresh_title_counts(title_id: str) -> None:
    database = get_db()
    count = await database.subtitles.count_documents({"title_id": title_id, "status": "published", "language": "Sinhala"})
    downloads = sum(
        row.get("download_count", 0)
        for row in await database.subtitles.find({"title_id": title_id}, {"download_count": 1}).to_list(length=5000)
    )
    await database.titles.update_one(
        {"_id": title_id},
        {"$set": {"subtitle_count": count, "download_count": downloads, "status": "published" if count else "review", "updated_at": utcnow()}},
    )


async def resolve_uploader(message: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    database = get_db()
    metadata = metadata or {}
    if metadata.get("uploader_id"):
        member = await database.members.find_one({"_id": metadata["uploader_id"]})
        if member:
            return member
    username = clean_username(metadata.get("uploader_username"))
    caption = getattr(message, "caption", "") or ""
    if not username:
        username = caption_uploader(caption)
    sender = getattr(message, "from_user", None)
    if not username and sender:
        username = clean_username(getattr(sender, "username", ""))
    if username:
        member = await database.members.find_one({"username": username, "active": True})
        if member:
            return member
    author_signature = (getattr(message, "author_signature", "") or "").strip()
    if author_signature:
        return await database.members.find_one({"display_name": author_signature, "active": True})
    return None


def _tmdb_search_variants(title_guess: str) -> list[str]:
    clean = re.sub(r"\s+", " ", title_guess or "").strip()
    without_year = re.sub(r"\b(?:19|20)\d{2}\b", "", clean).strip()
    return list(dict.fromkeys(value for value in (clean, without_year) if len(value) >= 2))


async def auto_tmdb_match(title_guess: str, is_series: bool) -> dict[str, Any] | None:
    """Auto-match only when the filename makes a confident, type-correct match."""
    try:
        expected_kind = "tv" if is_series else None
        wanted = "tv" if is_series else "all"
        for query in _tmdb_search_variants(title_guess):
            results = await tmdb_search(query, wanted, expected_kind=expected_kind)
            selected = results[0] if results else None
            if selected and is_confident_match(selected, query, expected_kind):
                return await tmdb_details(selected["type"], int(selected["id"]))
            if selected:
                logger.info("Auto TMDB match left for review: %r → %r (score=%s)", query, selected.get("name"), selected.get("match_score"))
    except TMDBError as error:
        logger.warning("Automatic TMDB match skipped: %s", error)
    return None


async def ingest_channel_message(message: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Import a Telegram channel document once. Bot-published captions carry reliable metadata."""
    database = get_db()
    document = getattr(message, "document", None)
    filename = getattr(document, "file_name", "") if document else ""
    if not document or not valid_subtitle_filename(filename):
        return None

    channel_id = str(getattr(getattr(message, "chat", None), "id", settings.auth_channel))
    message_id = int(getattr(message, "id", 0) or 0)
    if not message_id:
        return None
    existing = await database.subtitles.find_one({"channel_id": channel_id, "message_id": message_id})
    if existing:
        return existing

    metadata = metadata or {}
    caption = getattr(message, "caption", "") or ""
    guess = parse_subtitle_name(filename, caption)
    tmdb_info: dict[str, Any] | None = None
    supplied_type, supplied_id = metadata.get("tmdb_type"), metadata.get("tmdb_id")
    if supplied_type and supplied_id:
        try:
            tmdb_info = await tmdb_details(str(supplied_type), int(supplied_id))
        except (TMDBError, ValueError) as error:
            logger.warning("Provided TMDB reference could not be resolved: %s", error)
    if not tmdb_info:
        saved_ref = caption_tmdb(caption)
        if saved_ref:
            try:
                tmdb_info = await tmdb_details(saved_ref[0], saved_ref[1])
            except TMDBError:
                pass
    if not tmdb_info:
        tmdb_info = await auto_tmdb_match(guess.title_guess, bool(guess.season))

    title = await ensure_title(tmdb_info, guess.title_guess)
    uploader = await resolve_uploader(message, metadata)
    source_type = metadata.get("source_type") if metadata.get("source_type") in SOURCE_TYPES else (caption_source(caption) or guess.source_type)
    resolution = metadata.get("resolution") if metadata.get("resolution") in RESOLUTIONS else (caption_resolution(caption) or guess.resolution)
    note = str(metadata.get("note") or guess.note or "").strip()[:1000]
    season = metadata.get("season") if metadata.get("season") is not None else guess.season
    episode = metadata.get("episode") if metadata.get("episode") is not None else guess.episode
    status = metadata.get("status") if metadata.get("status") in {"published", "review", "hidden"} else ("published" if tmdb_info else "review")

    subtitle = {
        "_id": new_id("sub_"),
        "title_id": title["_id"],
        "channel_id": channel_id,
        "message_id": message_id,
        "file_id": getattr(document, "file_id", ""),
        "filename": filename,
        "file_size": int(getattr(document, "file_size", 0) or 0),
        "language": "Sinhala",
        "source_type": source_type,
        "resolution": resolution,
        "season": int(season) if str(season or "").isdigit() else None,
        "episode": int(episode) if str(episode or "").isdigit() else None,
        "episode_end": int(getattr(guess, "episode_end", 0) or 0) or None,
        "codec": getattr(guess, "codec", ""),
        "bit_depth": getattr(guess, "bit_depth", ""),
        "hdr": getattr(guess, "hdr", ""),
        "release_group": getattr(guess, "release_group", ""),
        "note": note,
        "uploader_id": uploader.get("_id") if uploader else None,
        "uploader_name": member_display(uploader) if uploader else (getattr(message, "author_signature", "") or "Channel contributor"),
        "uploader_username": uploader.get("username") if uploader else caption_uploader(caption),
        "status": status,
        "download_count": 0,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "imported_from": "bot" if metadata else "channel_auto",
    }
    try:
        await database.subtitles.insert_one(subtitle)
    except DuplicateKeyError:
        return await database.subtitles.find_one({"channel_id": channel_id, "message_id": message_id})
    await refresh_title_counts(title["_id"])
    return subtitle


# ─────────────────────────────────────────────────────────────────────────────
# Telegram member identity and bot workflow helpers
# ─────────────────────────────────────────────────────────────────────────────
def member_custom_name(value: Any) -> str:
    """Normalise a user-owned public team name while preserving Sinhala text."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:42]


def member_display(member: dict[str, Any] | None) -> str:
    if not member:
        return "Channel contributor"
    custom_name = member_custom_name(member.get("custom_name"))
    if custom_name:
        return custom_name
    if member.get("username"):
        return f"@{member['username']}"
    if member.get("display_name"):
        return str(member["display_name"])
    return "Team member"


def role_label(role: str) -> str:
    return {"owner": "Owner", "editor": "Editor", "maker": "Maker"}.get(role, "Member")


async def resolve_member_user(user: Any) -> dict[str, Any] | None:
    if not user:
        return None
    database = get_db()
    telegram_id = str(getattr(user, "id", ""))
    username = clean_username(getattr(user, "username", ""))
    display_name = " ".join(part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if part).strip() or username or "Telegram member"
    if username:
        await database.identities.update_one(
            {"username": username},
            {"$set": {"telegram_id": telegram_id, "display_name": display_name, "updated_at": utcnow()}},
            upsert=True,
        )

    by_id = await database.members.find_one({"telegram_id": telegram_id, "active": True})
    by_username = await database.members.find_one({"username": username, "active": True}) if username else None
    # A username may be added by an owner before that person opens the bot. Merge
    # this pending row with the OWNER_ID bootstrap row instead of leaving duplicates.
    if by_id and by_username and by_id["_id"] != by_username["_id"]:
        if by_id.get("telegram_id") == str(settings.owner_id) and by_username.get("role") == "owner":
            await database.members.delete_one({"_id": by_id["_id"]})
            member = by_username
        else:
            member = by_username
    else:
        member = by_id or by_username
    if not member:
        return None
    updates: dict[str, Any] = {"telegram_id": telegram_id, "updated_at": utcnow()}
    if username:
        updates["username"] = username
    if not member.get("display_name") or member.get("display_name") in {"Owner", "Telegram member"}:
        updates["display_name"] = display_name
    try:
        await database.members.update_one({"_id": member["_id"]}, {"$set": updates})
    except DuplicateKeyError:
        pass
    return await database.members.find_one({"_id": member["_id"]})


async def require_bot_member(message_or_callback: Any) -> dict[str, Any] | None:
    message = getattr(message_or_callback, "message", message_or_callback)
    member = await resolve_member_user(getattr(message_or_callback, "from_user", None) or getattr(message, "from_user", None))
    if member:
        return member
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id:
        await tg.send_text(
            chat_id,
            "<b>Member access required</b>\n\nAsk an owner to add your public Telegram username, then send <code>/start</code> again.",
            owner_contact_keyboard(),
        )
    return None


async def save_state(user_id: str, kind: str, payload: dict[str, Any]) -> None:
    await get_db().bot_states.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "kind": kind, "payload": payload, "updated_at": utcnow(), "expires_at": utcnow() + timedelta(minutes=20)}},
        upsert=True,
    )


async def pop_state(user_id: str) -> dict[str, Any] | None:
    state = await get_db().bot_states.find_one({"user_id": user_id, "expires_at": {"$gt": utcnow()}})
    if state:
        await get_db().bot_states.delete_one({"_id": state["_id"]})
    return state


async def current_state(user_id: str) -> dict[str, Any] | None:
    return await get_db().bot_states.find_one({"user_id": user_id, "expires_at": {"$gt": utcnow()}})


async def clear_state(user_id: str) -> None:
    await get_db().bot_states.delete_one({"user_id": user_id})


def draft_id() -> str:
    return secrets.token_urlsafe(8).replace("-", "a").replace("_", "b")[:10]


async def draft_for(key: str, user_id: str) -> dict[str, Any] | None:
    return await get_db().bot_drafts.find_one({"draft_key": key, "user_id": user_id, "expires_at": {"$gt": utcnow()}})


def pick_button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=callback_data)


def owner_contact_keyboard() -> InlineKeyboardMarkup:
    """Direct unapproved users to the configured project owner."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("👤 Contact Owner", url="https://t.me/soloRider_DC2")]]
    )

def draft_is_series(draft: dict[str, Any]) -> bool:
    """Detect TV drafts from the selected TMDB type or parsed episode details."""
    selected = draft.get("selected_tmdb") or {}
    return selected.get("tmdb_type") == "tv" or bool(draft.get("season") or draft.get("episode"))



def draft_keyboard(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    key = draft["draft_key"]
    selected = draft.get("selected_tmdb")
    if not selected:
        if draft_is_series(draft):
            return InlineKeyboardMarkup(
                [
                    [pick_button("📺 Search series", f"d:{key}:search:tv")],
                    [pick_button("🔎 Search all series first", f"d:{key}:search:all")],
                    [pick_button("✍️ Change title search", f"d:{key}:query"), pick_button("✖ Cancel", f"d:{key}:cancel")],
                ]
            )
        return InlineKeyboardMarkup(
            [
                [pick_button("🎬 Search movies", f"d:{key}:search:movie"), pick_button("📺 Search series", f"d:{key}:search:tv")],
                [pick_button("🔎 Search all", f"d:{key}:search:all")],
                [pick_button("✍️ Change title search", f"d:{key}:query"), pick_button("✖ Cancel", f"d:{key}:cancel")],
            ]
        )

    source_choices = list(SOURCE_TYPES)
    resolution_choices = list(RESOLUTIONS)
    current_source = draft.get("source_type", "Other")
    current_resolution = draft.get("resolution", "Other")
    source_buttons = [
        pick_button(("✓ " if source == current_source else "") + source, f"d:{key}:src:{index}")
        for index, source in enumerate(source_choices)
    ]
    resolution_buttons = [
        pick_button(("✓ " if value == current_resolution else "") + value, f"d:{key}:res:{index}")
        for index, value in enumerate(resolution_choices)
    ]
    confirm_label = "💾 Save changes" if draft.get("mode") == "edit" else "🚀 Send to channel"
    controls = [pick_button("✎ Edit release note" if draft.get("note") else "＋ Release note", f"d:{key}:note")]
    if selected.get("tmdb_type") == "movie":
        controls.append(pick_button("✎ Website card note" if draft.get("title_note_html") else "＋ Website card note", f"d:{key}:pagenote"))
    elif draft.get("season") and draft.get("episode"):
        controls.append(pick_button("✎ Episode page note" if draft.get("episode_note_touched") else "＋ Episode page note", f"d:{key}:epnote"))
    controls.append(pick_button("🔎 Change title", f"d:{key}:retitle"))
    control_rows = [controls[:2], controls[2:]] if len(controls) > 2 else [controls]
    return InlineKeyboardMarkup(
        [
            source_buttons[:3], source_buttons[3:6], source_buttons[6:9], source_buttons[9:],
            resolution_buttons[:3], resolution_buttons[3:6], resolution_buttons[6:],
            *[row for row in control_rows if row],
            [pick_button(confirm_label, f"d:{key}:publish")],
            [pick_button("✖ Cancel", f"d:{key}:cancel")],
        ]
    )


def draft_quality_label(draft: dict[str, Any]) -> str:
    values = [
        str(draft.get("source_type") or ""),
        str(draft.get("resolution") or ""),
        str(draft.get("codec") or ""),
        str(draft.get("bit_depth") or ""),
        str(draft.get("hdr") or ""),
    ]
    return " · ".join(value for value in values if value and value != "Other") or "Other"


def draft_card_text(draft: dict[str, Any]) -> str:
    filename = html.escape(draft.get("filename") or "Subtitle file")
    guessed = html.escape(draft.get("title_query") or "Untitled")
    selected = draft.get("selected_tmdb") or {}
    release = html.escape(draft_quality_label(draft))
    episode = ""
    if draft.get("season") and draft.get("episode"):
        suffix = f"–E{int(draft['episode_end']):02d}" if draft.get("episode_end") else ""
        episode = f"\n📺 Episode: <b>S{int(draft['season']):02d}E{int(draft['episode']):02d}{suffix}</b>"
    elif draft.get("season"):
        episode = f"\n📺 Season: <b>{int(draft['season'])}</b>"
    group = html.escape(str(draft.get("release_group") or ""))
    group_line = f"\n🏷 Release group: <b>{group}</b>" if group else ""
    if not selected:
        return (
            "<b>SUBTITLE SUBMISSION</b>\n\n"
            f"📁 <b>{filename}</b>\n"
            f"🔍 Detected title: <b>{guessed}</b>\n"
            f"⚙️ Detected release: <b>{release}</b>{episode}{group_line}\n\n"
            "Choose the exact title from TMDB before this file is sent to the subtitle channel."
        )
    year = selected.get("release_year") or "—"
    title = html.escape(selected.get("name") or "Untitled")
    kind = html.escape(selected.get("media_label") or ("Movie" if selected.get("tmdb_type") == "movie" else "Series"))
    note = html.escape(draft.get("note") or "No release note")
    website_note_line = ""
    episode_note_line = ""
    if selected.get("tmdb_type") == "movie":
        website_note_line = "\n🌐 Website card: <b>custom note ready</b>" if draft.get("title_note_html") else "\n🌐 Website card: TMDB details only"
    elif selected.get("tmdb_type") == "tv":
        if draft.get("season") and draft.get("episode"):
            episode_note_line = "\n📖 Episode page: <b>custom note ready</b>" if draft.get("episode_note") else "\n📖 Episode page: TMDB synopsis only"
        else:
            episode_note_line = "\n📖 Series notes: <b>set them on individual episode pages</b>"
    return (
        "<b>READY TO PUBLISH</b>\n\n"
        f"🎬 <b>{title}</b> <code>{year}</code>\n"
        f"📌 {kind} · ⭐ {selected.get('rating', 0):.1f}\n"
        f"📁 <b>{filename}</b>\n"
        f"⚙️ <b>{release}</b>{episode}{group_line}\n"
        f"📝 Release: {note}{website_note_line}{episode_note_line}\n\n"
        "Check the poster, title and release details. Then send it to the channel."
    )


async def show_draft(draft: dict[str, Any], previous_message_id: int | None = None) -> dict[str, Any]:
    selected = draft.get("selected_tmdb") or {}
    message = await tg.replace_card(
        int(draft["chat_id"]),
        draft_card_text(draft),
        keyboard=draft_keyboard(draft),
        poster_url=selected.get("poster_url", ""),
        previous_message_id=previous_message_id if previous_message_id is not None else draft.get("ui_message_id"),
    )
    await get_db().bot_drafts.update_one(
        {"_id": draft["_id"]},
        {"$set": {"ui_message_id": int(message.id), "updated_at": utcnow(), "expires_at": utcnow() + timedelta(hours=12)}},
    )
    return await get_db().bot_drafts.find_one({"_id": draft["_id"]})


async def show_tmdb_results(draft: dict[str, Any], kind: str, previous_message_id: int | None = None) -> None:
    query = str(draft.get("title_query") or "").strip()
    is_series = draft_is_series(draft)
    expected_kind = "tv" if is_series else None
    # An SxxExx filename cannot be a movie. Even the "all" tab is deliberately
    # constrained to TV results so a title containing one matching word cannot win.
    effective_kind = "tv" if is_series and kind == "all" else kind
    try:
        results = await tmdb_search(query, effective_kind, expected_kind=expected_kind)
    except TMDBError as error:
        await tg.send_text(int(draft["chat_id"]), f"<b>TMDB search failed</b>\n{html.escape(str(error))}")
        return
    if not results:
        await tg.send_text(int(draft["chat_id"]), "No TMDB results found. Choose <b>Change title search</b> and try a shorter exact name.")
        return
    await get_db().bot_drafts.update_one(
        {"_id": draft["_id"]},
        {"$set": {"tmdb_kind": effective_kind, "tmdb_results": results, "updated_at": utcnow(), "expires_at": utcnow() + timedelta(hours=12)}},
    )
    key = draft["draft_key"]
    rows: list[list[InlineKeyboardButton]] = []
    for item in results[:10]:
        label = f"{'🎬' if item['type'] == 'movie' else '📺'} {item['name']} ({item.get('year') or '—'})"
        rows.append([pick_button(label[:58], f"d:{key}:pick:{item['type']}:{item['id']}")])
    rows.append([pick_button("✍️ Change title search", f"d:{key}:query"), pick_button("✖ Cancel", f"d:{key}:cancel")])
    type_hint = "\n\nDetected <b>episode file</b>: series-only results are shown first." if is_series else ""
    text = f"<b>TMDB RESULTS</b>\n\nSearch: <b>{html.escape(query)}</b>{type_hint}\nSelect the exact title. The next card will show its poster."
    message = await tg.replace_card(int(draft["chat_id"]), text, InlineKeyboardMarkup(rows), previous_message_id=previous_message_id or draft.get("ui_message_id"))
    await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": {"ui_message_id": int(message.id), "updated_at": utcnow()}})


def channel_caption(draft: dict[str, Any], member: dict[str, Any]) -> str:
    """Build the short, consistent caption shown in the subtitle channel.

    The caption intentionally stays human-readable and uses the same source
    line for every release. Full TMDB, quality, note and team data are stored
    with the subtitle record when the bot publishes the file.
    """
    title = draft["selected_tmdb"]
    filename = html.escape(draft.get("filename") or "subtitle")
    media_kind = title.get("tmdb_type") == "movie"
    name = html.escape(title.get("name") or "Untitled")
    year = str(title.get("release_year") or title.get("year") or "").strip()
    media_title = f"{name} ({html.escape(year)})" if year else name

    # The header always uses the current uploader profile. It is intentionally
    # not configurable at deployment level, so it cannot overwrite a member's
    # own display name or username.
    caption_name = html.escape(member.get("display_name") or settings.site_name)
    caption_username = clean_username(member.get("username"))
    author_line = f"{caption_name} (@{caption_username})" if caption_username else caption_name

    if media_kind:
        release_line = f"└ 🎬 Movie : {filename}"
    else:
        episode = ""
        if draft.get("season") and draft.get("episode"):
            suffix = f"–E{int(draft['episode_end']):02d}" if draft.get("episode_end") else ""
            episode = f"S{int(draft['season']):02d}E{int(draft['episode']):02d}{suffix}"
        series_parts = [media_title]
        if episode:
            series_parts.append(episode)
        series_parts.append("Sinhala [si]")
        series_parts.append(filename)
        release_line = f"└ 📺 Series : {' - '.join(series_parts)}"

    return f"{author_line}\n────────────────\n{release_line}"[:3900]


def saved_channel_caption(subtitle: dict[str, Any], title: dict[str, Any]) -> str:
    """Rebuild the standard channel caption when refreshing an existing post."""
    selected = {
        "tmdb_type": title.get("tmdb_type"),
        "tmdb_id": title.get("tmdb_id"),
        "name": title.get("name") or "Untitled",
        "release_year": title.get("release_year"),
    }
    draft = {
        "selected_tmdb": selected,
        "filename": subtitle.get("filename") or "subtitle.srt",
        "season": subtitle.get("season"),
        "episode": subtitle.get("episode"),
        "episode_end": subtitle.get("episode_end"),
    }
    maker = {
        "display_name": subtitle.get("uploader_name") or settings.site_name,
        "username": subtitle.get("uploader_username") or "",
    }
    return channel_caption(draft, maker)


def saved_channel_keyboard(subtitle: dict[str, Any], title: dict[str, Any]) -> InlineKeyboardMarkup | None:
    return channel_page_keyboard(title, subtitle.get("season"), subtitle.get("episode"))


async def create_new_draft(message: Message, member: dict[str, Any]) -> None:
    document = message.document
    filename = document.file_name or "subtitle.srt"
    if not valid_subtitle_filename(filename):
        extensions = ", ".join(sorted(ALLOWED_EXTENSIONS))
        await message.reply_text(f"Send only subtitle files: <code>{extensions}</code>", parse_mode="html")
        return
    guess = parse_subtitle_name(filename, message.caption or "")
    user_id = str(message.from_user.id)
    await get_db().bot_drafts.delete_many({"user_id": user_id, "mode": "new"})
    draft = {
        "_id": new_id("draft_"),
        "draft_key": draft_id(),
        "mode": "new",
        "user_id": user_id,
        "member_id": member["_id"],
        "chat_id": int(message.chat.id),
        "source_chat_id": int(message.chat.id),
        "source_message_id": int(message.id),
        "file_id": document.file_id,
        "filename": filename,
        "file_size": int(document.file_size or 0),
        "title_query": guess.title_guess,
        "source_type": guess.source_type,
        "resolution": guess.resolution,
        "season": guess.season,
        "episode": guess.episode,
        "episode_end": getattr(guess, "episode_end", None),
        "codec": getattr(guess, "codec", ""),
        "bit_depth": getattr(guess, "bit_depth", ""),
        "hdr": getattr(guess, "hdr", ""),
        "release_group": getattr(guess, "release_group", ""),
        "note": guess.note,
        "title_note_html": "",
        "title_note_touched": False,
        "episode_note": "",
        "episode_note_touched": False,
        "selected_tmdb": None,
        "tmdb_results": [],
        "ui_message_id": None,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "expires_at": utcnow() + timedelta(hours=12),
    }
    await get_db().bot_drafts.insert_one(draft)
    await show_draft(draft)


async def edit_draft_from_subtitle(chat_id: int, user_id: str, member: dict[str, Any], subtitle: dict[str, Any]) -> None:
    title = await get_db().titles.find_one({"_id": subtitle.get("title_id")})
    selected: dict[str, Any] | None = None
    if title and title.get("tmdb_id") and title.get("tmdb_type") in {"movie", "tv"}:
        selected = {
            "tmdb_id": title["tmdb_id"],
            "tmdb_type": title["tmdb_type"],
            "name": title.get("name") or "Untitled",
            "media_label": title.get("media_label") or ("Movie" if title["tmdb_type"] == "movie" else "Series"),
            "release_year": title.get("release_year"),
            "poster_url": title.get("poster_url", ""),
            "backdrop_url": title.get("backdrop_url", ""),
            "rating": title.get("rating", 0),
            "overview": title.get("overview", ""),
            "cast": title.get("cast", []),
        }
    existing_title_note = title_page_note_html((title or {}).get("page_note_html")) if (title or {}).get("tmdb_type") == "movie" else ""
    existing_episode_note = ""
    if title and title.get("tmdb_type") == "tv" and subtitle.get("season") and subtitle.get("episode"):
        page = await get_db().episode_pages.find_one(
            {"_id": episode_page_id(str(title["_id"]), int(subtitle["season"]), int(subtitle["episode"]))}
        )
        existing_episode_note = str((page or {}).get("manual_note_html") or (page or {}).get("manual_note") or "")
    draft = {
        "_id": new_id("draft_"),
        "draft_key": draft_id(),
        "mode": "edit",
        "subtitle_id": subtitle["_id"],
        "user_id": user_id,
        "member_id": member["_id"],
        "chat_id": chat_id,
        "filename": subtitle.get("filename", "subtitle.srt"),
        "file_size": subtitle.get("file_size", 0),
        "title_query": (title or {}).get("name") or parse_subtitle_name(subtitle.get("filename", "")).title_guess,
        "source_type": subtitle.get("source_type") or "Other",
        "resolution": subtitle.get("resolution") or "Other",
        "season": subtitle.get("season"),
        "episode": subtitle.get("episode"),
        "episode_end": subtitle.get("episode_end"),
        "codec": subtitle.get("codec") or "",
        "bit_depth": subtitle.get("bit_depth") or "",
        "hdr": subtitle.get("hdr") or "",
        "release_group": subtitle.get("release_group") or "",
        "note": subtitle.get("note") or "",
        "title_note_html": existing_title_note,
        "title_note_touched": False,
        "episode_note": existing_episode_note,
        "episode_note_touched": False,
        "selected_tmdb": selected,
        "original_uploader_id": subtitle.get("uploader_id"),
        "original_uploader_name": subtitle.get("uploader_name"),
        "original_uploader_username": subtitle.get("uploader_username"),
        "ui_message_id": None,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "expires_at": utcnow() + timedelta(hours=12),
    }
    await get_db().bot_drafts.insert_one(draft)
    await show_draft(draft)


async def publish_new_draft(draft: dict[str, Any], member: dict[str, Any], callback: CallbackQuery) -> None:
    selected = draft.get("selected_tmdb")
    if not selected:
        await tg.answer(callback, "Choose a TMDB title first.", alert=True)
        return
    caption = channel_caption(draft, member)
    page_keyboard = channel_page_keyboard(selected, draft.get("season"), draft.get("episode"))
    try:
        channel_message = await tg.copy_document_to_channel(
            int(draft["source_chat_id"]),
            int(draft["source_message_id"]),
            str(draft["file_id"]),
            caption,
            page_keyboard,
        )
        record = await ingest_channel_message(
            channel_message,
            {
                "uploader_id": member["_id"],
                "uploader_username": member.get("username", ""),
                "tmdb_type": selected["tmdb_type"],
                "tmdb_id": selected["tmdb_id"],
                "source_type": draft.get("source_type"),
                "resolution": draft.get("resolution"),
                "season": draft.get("season"),
                "episode": draft.get("episode"),
                "episode_end": draft.get("episode_end"),
                "codec": draft.get("codec") or "",
                "bit_depth": draft.get("bit_depth") or "",
                "hdr": draft.get("hdr") or "",
                "release_group": draft.get("release_group") or "",
                "note": draft.get("note"),
                "status": "published",
            },
        )
    except TelegramStorageError as error:
        await tg.answer(callback, "Telegram storage failed. Retry after checking channel access.", alert=True)
        await tg.send_text(int(draft["chat_id"]), f"<b>Could not send to the channel.</b>\n{html.escape(str(error))}\n\nYour draft is kept. Tap <b>Send to channel</b> again.")
        return
    except Exception:
        logger.exception("Bot publish failed")
        await tg.answer(callback, "Publishing failed. The draft was kept.", alert=True)
        return

    if record:
        saved_title = await get_db().titles.find_one({"_id": record.get("title_id")})
        if saved_title:
            await save_draft_title_note(draft, saved_title, member)
            await save_draft_episode_note(draft, saved_title, member)
    await get_db().bot_drafts.delete_one({"_id": draft["_id"]})
    await clear_state(draft["user_id"])
    title = selected.get("name") or "subtitle"
    text = (
        "<b>SUBTITLE PUBLISHED</b>\n\n"
        f"🎬 <b>{html.escape(title)}</b>\n"
        f"📁 {html.escape(draft['filename'])}\n"
        f"🏷 {html.escape(draft.get('source_type') or 'Other')} · {html.escape(draft.get('resolution') or 'Other')}\n"
        f"👤 Saved as {html.escape(member_display(member))}\n\n"
        "The file is now stored in the subtitle channel and visible on the website."
    )
    await tg.replace_card(int(draft["chat_id"]), text, previous_message_id=getattr(callback.message, "id", None))
    logger.info("Published bot subtitle: %s (%s)", draft["filename"], (record or {}).get("_id", "stored"))


async def save_edit_draft(draft: dict[str, Any], member: dict[str, Any], callback: CallbackQuery) -> None:
    selected = draft.get("selected_tmdb")
    if not selected:
        await tg.answer(callback, "Choose a TMDB title first.", alert=True)
        return
    subtitle = await get_db().subtitles.find_one({"_id": draft.get("subtitle_id")})
    if not subtitle:
        await tg.answer(callback, "This subtitle no longer exists.", alert=True)
        return
    if subtitle.get("uploader_id") != member["_id"] and member.get("role") not in {"owner", "editor"}:
        await tg.answer(callback, "You can edit only your own files.", alert=True)
        return
    title = await ensure_title(selected, selected.get("name") or "Untitled")
    caption = channel_caption(
        {**draft, "selected_tmdb": selected},
        await get_db().members.find_one({"_id": subtitle.get("uploader_id")}) or member,
    )
    page_keyboard = channel_page_keyboard(selected, draft.get("season"), draft.get("episode"))
    try:
        await tg.edit_channel_caption(int(subtitle["message_id"]), caption, page_keyboard)
    except TelegramStorageError as error:
        await tg.answer(callback, "Telegram caption update failed.", alert=True)
        await tg.send_text(int(draft["chat_id"]), f"<b>Could not update channel details.</b>\n{html.escape(str(error))}")
        return

    old_title_id = subtitle.get("title_id")
    await get_db().subtitles.update_one(
        {"_id": subtitle["_id"]},
        {
            "$set": {
                "title_id": title["_id"],
                "source_type": draft.get("source_type") or "Other",
                "resolution": draft.get("resolution") or "Other",
                "season": draft.get("season"),
                "episode": draft.get("episode"),
                "episode_end": draft.get("episode_end"),
                "codec": draft.get("codec") or "",
                "bit_depth": draft.get("bit_depth") or "",
                "hdr": draft.get("hdr") or "",
                "release_group": draft.get("release_group") or "",
                "note": draft.get("note") or "",
                "status": "published",
                "updated_at": utcnow(),
            }
        },
    )
    if old_title_id and old_title_id != title["_id"]:
        await refresh_title_counts(old_title_id)
    await refresh_title_counts(title["_id"])
    await save_draft_title_note(draft, title, member)
    await save_draft_episode_note(draft, title, member)
    await get_db().bot_drafts.delete_one({"_id": draft["_id"]})
    await clear_state(draft["user_id"])
    text = f"<b>FILE UPDATED</b>\n\n🎬 <b>{html.escape(selected['name'])}</b>\n📁 {html.escape(subtitle['filename'])}\n\nThe website and channel caption now use the corrected details."
    await tg.replace_card(int(draft["chat_id"]), text, previous_message_id=getattr(callback.message, "id", None))


def can_manage_subtitle(member: dict[str, Any], subtitle: dict[str, Any]) -> bool:
    """Makers manage their own files; editors and owners manage the team library."""
    return subtitle.get("uploader_id") == member.get("_id") or member.get("role") in {"owner", "editor"}


def subtitle_file_card_text(subtitle: dict[str, Any], title: dict[str, Any] | None = None) -> str:
    title = title or {}
    release = html.escape(draft_quality_label(subtitle))
    filename = html.escape(str(subtitle.get("filename") or "subtitle.srt"))
    episode = ""
    if subtitle.get("season") and subtitle.get("episode"):
        end = f"–E{int(subtitle['episode_end']):02d}" if subtitle.get("episode_end") else ""
        episode = f"\n📺 Episode: <b>S{int(subtitle['season']):02d}E{int(subtitle['episode']):02d}{end}</b>"
    status = "Published" if subtitle.get("status") == "published" else str(subtitle.get("status") or "Review").title()
    return (
        "<b>SUBTITLE FILE</b>\n\n"
        f"🎬 <b>{html.escape(str(title.get('name') or 'Untitled'))}</b>\n"
        f"📁 {filename}\n"
        f"⚙️ {release}{episode}\n"
        f"📌 Status: <b>{html.escape(status)}</b>\n\n"
        "Edit the details, or permanently delete this existing subtitle from the website and storage channel."
    )


async def show_subtitle_file_card(callback: CallbackQuery, member: dict[str, Any], subtitle_id: str, scope: str = "mine") -> None:
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id})
    if not subtitle:
        await tg.answer(callback, "File not found.", alert=True)
        return
    if not can_manage_subtitle(member, subtitle):
        await tg.answer(callback, "You can manage only your own files.", alert=True)
        return
    title = await get_db().titles.find_one({"_id": subtitle.get("title_id")})
    back = f"f:list:{scope}"
    rows = [
        [pick_button("✎ Edit details", f"f:edit:{subtitle['_id']}:{scope}")],
        [pick_button("🔗 Refresh channel button", f"f:refresh:{subtitle['_id']}:{scope}")],
        [pick_button("🗑 Delete subtitle", f"f:delete:{subtitle['_id']}:{scope}")],
        [pick_button("← Back to files", back)],
    ]
    await tg.answer(callback)
    await tg.replace_card(
        int(callback.message.chat.id),
        subtitle_file_card_text(subtitle, title),
        InlineKeyboardMarkup(rows),
        previous_message_id=callback.message.id,
    )


async def refresh_existing_channel_button(callback: CallbackQuery, member: dict[str, Any], subtitle_id: str, scope: str = "mine") -> None:
    """Replace legacy duplicate channel buttons with one current page button."""
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id})
    if not subtitle:
        await tg.answer(callback, "File not found.", alert=True)
        return
    if not can_manage_subtitle(member, subtitle):
        await tg.answer(callback, "You can manage only your own files.", alert=True)
        return
    title = await get_db().titles.find_one({"_id": subtitle.get("title_id")})
    if not title:
        await tg.answer(callback, "Title details are missing.", alert=True)
        return
    keyboard = saved_channel_keyboard(subtitle, title)
    if not keyboard:
        await tg.answer(callback, "Set PUBLIC_BASE_URL first, then refresh this channel post.", alert=True)
        return
    message_id = int(subtitle.get("message_id") or 0)
    if not message_id:
        await tg.answer(callback, "The channel post is missing.", alert=True)
        return
    try:
        await tg.edit_channel_caption(message_id, saved_channel_caption(subtitle, title), keyboard)
    except TelegramStorageError as error:
        await tg.answer(callback, "Channel update failed.", alert=True)
        await tg.send_text(int(callback.message.chat.id), f"<b>Could not refresh the channel post.</b>\n{html.escape(str(error))}")
        return
    await tg.answer(callback, "Channel button refreshed.")
    await tg.replace_card(
        int(callback.message.chat.id),
        "<b>CHANNEL BUTTON UPDATED</b>\n\n"
        "The old duplicate buttons were replaced with one page button. It opens the movie page or exact episode page with its available subtitle files.",
        InlineKeyboardMarkup([[pick_button("← Back to file", f"f:view:{subtitle['_id']}:{scope}")]]),
        previous_message_id=callback.message.id,
    )


async def show_subtitle_delete_confirmation(callback: CallbackQuery, member: dict[str, Any], subtitle_id: str, scope: str = "mine") -> None:
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id})
    if not subtitle:
        await tg.answer(callback, "File not found.", alert=True)
        return
    if not can_manage_subtitle(member, subtitle):
        await tg.answer(callback, "You can delete only your own files.", alert=True)
        return
    filename = html.escape(str(subtitle.get("filename") or "subtitle.srt"))
    text = (
        "<b>DELETE SUBTITLE?</b>\n\n"
        f"📁 <b>{filename}</b>\n\n"
        "This permanently removes the subtitle from the website, database, and Telegram storage channel. "
        "This cannot be undone."
    )
    rows = [
        [pick_button("🗑 Yes, delete permanently", f"f:confirmdelete:{subtitle['_id']}:{scope}")],
        [pick_button("← Keep this subtitle", f"f:view:{subtitle['_id']}:{scope}")],
    ]
    await tg.answer(callback)
    await tg.replace_card(
        int(callback.message.chat.id),
        text,
        InlineKeyboardMarkup(rows),
        previous_message_id=callback.message.id,
    )


async def delete_existing_subtitle(callback: CallbackQuery, member: dict[str, Any], subtitle_id: str, scope: str = "mine") -> None:
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id})
    if not subtitle:
        await tg.answer(callback, "This subtitle has already been removed.", alert=True)
        return
    if not can_manage_subtitle(member, subtitle):
        await tg.answer(callback, "You can delete only your own files.", alert=True)
        return

    message_id = int(subtitle.get("message_id") or 0)
    if message_id:
        try:
            await tg.delete_channel_message(message_id)
        except TelegramStorageError as error:
            await tg.answer(callback, "Channel delete failed.", alert=True)
            await tg.send_text(
                int(callback.message.chat.id),
                f"<b>Subtitle was not deleted.</b>\n\nTelegram storage must delete the channel post first.\n{html.escape(str(error))}",
            )
            return

    title_id = str(subtitle.get("title_id") or "")
    await get_db().subtitles.delete_one({"_id": subtitle["_id"]})
    await get_db().reports.delete_many({"subtitle_id": subtitle["_id"]})
    if title_id:
        await refresh_title_counts(title_id)

    filename = html.escape(str(subtitle.get("filename") or "subtitle.srt"))
    await tg.answer(callback, "Subtitle deleted.")
    await tg.replace_card(
        int(callback.message.chat.id),
        "<b>SUBTITLE DELETED</b>\n\n"
        f"🗑 {filename}\n\n"
        "The file was removed from the storage channel and is no longer available on the website.",
        InlineKeyboardMarkup([[pick_button("📚 Team library" if scope == "team" else "🗂 My files", f"f:list:{scope}")]]),
        previous_message_id=callback.message.id,
    )


async def show_my_files(
    chat_id: int,
    member: dict[str, Any],
    all_files: bool = False,
    previous_message_id: int | None = None,
) -> None:
    query: dict[str, Any] = {} if all_files else {"uploader_id": member["_id"]}
    records = await get_db().subtitles.find(query).sort("updated_at", -1).to_list(length=12)
    if not records:
        message = "<b>NO FILES YET</b>\n\nSend an <code>.srt</code>, <code>.ass</code>, <code>.vtt</code>, or <code>.zip</code> file here to publish your first subtitle."
        if previous_message_id:
            await tg.replace_card(chat_id, message, previous_message_id=previous_message_id)
        else:
            await tg.send_text(chat_id, message)
        return
    title_ids = [item.get("title_id") for item in records]
    titles = {item["_id"]: item for item in await get_db().titles.find({"_id": {"$in": title_ids}}).to_list(length=30)}
    rows: list[list[InlineKeyboardButton]] = []
    for item in records:
        title = titles.get(item.get("title_id"), {})
        label = f"{'⚠️' if item.get('status') != 'published' else '📝'} {title.get('name') or item.get('filename')[:28]}"
        scope = "team" if all_files else "mine"
        rows.append([pick_button(label[:58], f"f:view:{item['_id']}:{scope}")])
    rows.append([pick_button("➕ Submit a subtitle", "h:submit")])
    head = "TEAM LIBRARY" if all_files else "MY SUBTITLES"
    text = f"<b>{head}</b>\n\nChoose an existing subtitle to edit its details or delete it permanently."
    keyboard = InlineKeyboardMarkup(rows)
    if previous_message_id:
        await tg.replace_card(chat_id, text, keyboard, previous_message_id=previous_message_id)
    else:
        await tg.send_text(chat_id, text, keyboard)


async def open_subtitle_editor(callback: CallbackQuery, member: dict[str, Any], subtitle_id: str) -> None:
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id})
    if not subtitle:
        await tg.answer(callback, "File not found.", alert=True)
        return
    if subtitle.get("uploader_id") != member["_id"] and member.get("role") not in {"owner", "editor"}:
        await tg.answer(callback, "You can edit only your own files.", alert=True)
        return
    await edit_draft_from_subtitle(int(callback.message.chat.id), str(callback.from_user.id), member, subtitle)
    await tg.answer(callback, "Editor opened.")


async def add_member_from_username(username_raw: str, role: str) -> dict[str, Any]:
    username = clean_username(username_raw)
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        raise ValueError("Send a valid public Telegram username, for example @yourname.")
    if role not in {"owner", "editor", "maker"}:
        raise ValueError("Choose a valid role.")
    database = get_db()
    identity = await database.identities.find_one({"username": username})
    existing = await database.members.find_one({"username": username})
    payload = {
        "username": username,
        "display_name": (identity or {}).get("display_name") or username,
        "role": role,
        "active": True,
        "updated_at": utcnow(),
    }
    if identity and identity.get("telegram_id"):
        payload["telegram_id"] = identity["telegram_id"]
    if existing:
        await database.members.update_one({"_id": existing["_id"]}, {"$set": payload})
        return await database.members.find_one({"_id": existing["_id"]})
    payload.update({"_id": new_id("member_"), "telegram_id": payload.get("telegram_id"), "custom_name": "", "avatar_url": "", "bio": "", "joined_at": utcnow()})
    await database.members.insert_one(payload)
    return payload


async def set_member_custom_name(target: dict[str, Any], raw_name: str) -> dict[str, Any]:
    """Save a member-owned public display name and refresh their existing files.

    The name is deliberately separate from Telegram's account name. Telegram
    identity updates continue normally while the team member's chosen credit
    remains stable across the subtitle library.
    """
    custom_name = member_custom_name(raw_name)
    now = utcnow()
    await get_db().members.update_one(
        {"_id": target["_id"]},
        {"$set": {"custom_name": custom_name, "updated_at": now}},
    )
    updated = await get_db().members.find_one({"_id": target["_id"]}) or target
    await get_db().subtitles.update_many(
        {"uploader_id": target["_id"]},
        {"$set": {"uploader_name": member_display(updated), "updated_at": now}},
    )
    return updated


async def show_members(chat_id: int, previous_message_id: int | None = None) -> None:
    members = await get_db().members.find({}).sort([("role", 1), ("updated_at", -1)]).to_list(length=40)
    rows: list[list[InlineKeyboardButton]] = [[pick_button("➕ Add member", "m:add")]]
    for entry in members:
        mark = {"owner": "👑", "editor": "🛠", "maker": "✦"}.get(entry.get("role"), "•")
        custom = member_custom_name(entry.get("custom_name"))
        username = f"@{entry['username']}" if entry.get("username") else "No username"
        name = f"{custom} · {username}" if custom else username
        if not entry.get("active"):
            name = f"{name} · off"
        rows.append([pick_button(f"{mark} {name}"[:58], f"m:view:{entry['_id']}")])
    text = "<b>MEMBER ACCESS</b>\n\nAdd a Telegram username, set a custom public name, change roles, deactivate access, or remove a member."
    if previous_message_id:
        await tg.replace_card(chat_id, text, InlineKeyboardMarkup(rows), previous_message_id=previous_message_id)
    else:
        await tg.send_text(chat_id, text, InlineKeyboardMarkup(rows))


async def show_member_card(callback: CallbackQuery, target_id: str) -> None:
    target = await get_db().members.find_one({"_id": target_id})
    if not target:
        await tg.answer(callback, "Member not found.", alert=True)
        return
    username = f"@{target['username']}" if target.get("username") else "No username linked yet"
    state = "Active" if target.get("active") else "Inactive"
    custom = member_custom_name(target.get("custom_name")) or "Not set"
    protected = target.get("telegram_id") == str(settings.owner_id)
    text = (
        "<b>MEMBER ACCESS</b>\n\n"
        f"👤 <b>{html.escape(member_display(target))}</b>\n"
        f"✎ Custom name: <b>{html.escape(custom)}</b>\n"
        f"📨 {html.escape(username)}\n"
        f"🛡 Role: <b>{role_label(target.get('role', 'maker'))}</b>\n"
        f"🔐 Status: <b>{state}</b>\n\n"
        + ("The configured owner is protected from removal and deactivation." if protected else "Only owners can manage this member. The member can change only their own custom name.")
    )
    rows: list[list[InlineKeyboardButton]] = [[pick_button("✎ Custom name", f"m:name:{target_id}")]]
    if not protected:
        rows.extend(
            [
                [pick_button("👑 Owner", f"m:set:{target_id}:owner"), pick_button("🛠 Editor", f"m:set:{target_id}:editor")],
                [pick_button("✦ Maker", f"m:set:{target_id}:maker"), pick_button("🔒 Toggle access", f"m:toggle:{target_id}")],
                [pick_button("🗑 Remove member", f"m:remove:{target_id}")],
            ]
        )
    rows.append([pick_button("← Back", "m:list")])
    await tg.replace_card(int(callback.message.chat.id), text, InlineKeyboardMarkup(rows), previous_message_id=callback.message.id)


async def show_member_remove_confirmation(callback: CallbackQuery, target_id: str) -> None:
    target = await get_db().members.find_one({"_id": target_id})
    if not target:
        await tg.answer(callback, "Member not found.", alert=True)
        return
    if target.get("telegram_id") == str(settings.owner_id):
        await tg.answer(callback, "The configured owner cannot be removed.", alert=True)
        return
    name = html.escape(member_display(target))
    text = (
        "<b>REMOVE MEMBER?</b>\n\n"
        f"🗑 <b>{name}</b> will lose access to this bot immediately.\n\n"
        "Their published subtitle files remain available and keep their current credit. This action does not delete subtitle files."
    )
    rows = [[pick_button("🗑 Yes, remove", f"m:confirmremove:{target_id}"), pick_button("Cancel", f"m:view:{target_id}")]]
    await tg.replace_card(int(callback.message.chat.id), text, InlineKeyboardMarkup(rows), previous_message_id=callback.message.id)


async def show_reports(chat_id: int) -> None:
    reports = await get_db().reports.find({"status": "open"}).sort("created_at", -1).to_list(length=10)
    if not reports:
        await tg.send_text(chat_id, "<b>NO OPEN REPORTS</b>\n\nViewer reports will appear here when a subtitle needs attention.")
        return
    rows: list[list[InlineKeyboardButton]] = []
    text = "<b>OPEN REPORTS</b>\n\n"
    for index, report in enumerate(reports, 1):
        subtitle = await get_db().subtitles.find_one({"_id": report.get("subtitle_id")}, {"filename": 1})
        text += f"{index}. <b>{html.escape((subtitle or {}).get('filename') or 'Deleted file')}</b>\n{html.escape(report.get('reason') or '')}\n\n"
        rows.append([pick_button(f"✓ Close report {index}", f"r:close:{report['_id']}")])
    await tg.send_text(chat_id, text[:3900], InlineKeyboardMarkup(rows))


async def show_ads(chat_id: int) -> None:
    records = await get_db().settings.find({"type": "ad"}).to_list(length=10)
    by_slot = {record.get("slot"): record for record in records}
    slots = (("site_bar_top", "Top nav bar top"), ("site_bar_bottom", "Top nav bar bottom"), ("home_top", "Home banner"), ("browse_inline", "Browse inline"), ("title_inline", "Title inline"))
    lines = ["<b>WEBSITE ADS</b>", "", "Set a network snippet through Telegram or disable a placement."]
    rows: list[list[InlineKeyboardButton]] = []
    for slot, label in slots:
        status = "ON" if by_slot.get(slot, {}).get("enabled") else "OFF"
        lines.append(f"• <b>{label}</b>: {status}")
        rows.append([pick_button(f"✎ {label}", f"a:slot:{slot}"), pick_button(f"{'◉' if status == 'ON' else '○'} Disable", f"a:off:{slot}")])
    await tg.send_text(chat_id, "\n".join(lines), InlineKeyboardMarkup(rows))


async def begin_channel_link(chat_id: int, user_id: str) -> None:
    """Start the one-time private-channel peer linking flow for an owner."""
    await save_state(user_id, "channel_connect", {})
    await tg.send_text(
        chat_id,
        "<b>LINK SUBTITLE STORAGE CHANNEL</b>\n\n"
        "Forward <b>any existing post</b> from your configured subtitle storage channel to this bot now. "
        "Do not copy or re-send it: use Telegram’s <b>Forward</b> action.\n\n"
        "The bot will verify AUTH_CHANNEL, save its private peer safely, and confirm when publishing is ready. "
        "Use <code>/cancel</code> to stop.",
    )


async def welcome(chat_id: int, member: dict[str, Any] | None) -> None:
    bot_heading = html.escape(APP_NAME.upper())
    if not member:
        await tg.send_text(
            chat_id,
            f"<b>{bot_heading}</b>\n\nThis bot is for the subtitle team. Ask an owner to add your public Telegram username, then send <code>/start</code> again.",
            owner_contact_keyboard(),
        )
        return
    role = role_label(member.get("role", "maker"))
    rows = [
        [pick_button("➕ Submit subtitle", "h:submit"), pick_button("🗂 My files", "h:myfiles")],
        [pick_button("✎ My custom name", "h:myname")],
    ]
    if member.get("role") in {"owner", "editor"}:
        rows.append([pick_button("📚 Team library", "h:library"), pick_button("🚩 Reports", "h:reports")])
    if member.get("role") == "owner":
        rows.append([pick_button("👥 Members", "h:members"), pick_button("📣 Website ads", "h:ads")])
        rows.append([pick_button("🔗 Link storage channel", "h:connectchannel")])
    text = (
        f"<b>{bot_heading}</b>\n\n"
        f"Welcome, <b>{html.escape(member_display(member))}</b>\n"
        f"Role: <b>{role}</b>\n\n"
        "Send a subtitle file here. The bot will show TMDB results and poster, then lets you choose the source, resolution and note before publishing to the channel."
    )
    await tg.send_text(chat_id, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────────────────────────────────────
# Telegram callbacks
# ─────────────────────────────────────────────────────────────────────────────
async def on_channel_message(message: Message) -> None:
    try:
        record = await ingest_channel_message(message)
        if record:
            logger.info("Auto-imported channel subtitle: %s", record["filename"])
    except Exception:
        logger.exception("Automatic channel subtitle import failed")


async def on_private_command(message: Message) -> None:
    member = await resolve_member_user(message.from_user)
    command = (message.command or ["start"])[0].lower()
    chat_id = int(message.chat.id)
    if command in {"start", "help"}:
        await welcome(chat_id, member)
        return
    if command == "cancel":
        await clear_state(str(message.from_user.id))
        await get_db().bot_drafts.delete_many({"user_id": str(message.from_user.id)})
        await message.reply_text("Current action cancelled.")
        return
    if not member:
        await welcome(chat_id, None)
        return
    if command == "submit":
        await tg.send_text(chat_id, "Send your <code>.srt</code>, <code>.ass</code>, <code>.ssa</code>, <code>.vtt</code>, or <code>.zip</code> file now. I will detect the release details and open the title menu.")
    elif command == "myfiles":
        await show_my_files(chat_id, member)
    elif command == "library":
        if member.get("role") not in {"owner", "editor"}:
            await tg.send_text(chat_id, "The team library is available to editors and owners. Use <code>/myfiles</code> for your own files.")
        else:
            await show_my_files(chat_id, member, all_files=True)
    elif command == "members":
        if member.get("role") != "owner":
            await tg.send_text(chat_id, "Only owners can manage member access.")
        else:
            await show_members(chat_id)
    elif command == "reports":
        if member.get("role") not in {"owner", "editor"}:
            await tg.send_text(chat_id, "Only editors and owners can review reports.")
        else:
            await show_reports(chat_id)
    elif command == "ads":
        if member.get("role") != "owner":
            await tg.send_text(chat_id, "Only owners can manage website ads.")
        else:
            await show_ads(chat_id)
    elif command == "connectchannel":
        if member.get("role") != "owner":
            await tg.send_text(chat_id, "Only owners can link the subtitle storage channel.")
        else:
            await begin_channel_link(chat_id, str(message.from_user.id))


async def on_private_document(message: Message) -> None:
    user_id = str(message.from_user.id)
    link_state = await current_state(user_id)
    if link_state and link_state.get("kind") == "channel_connect":
        linked, detail = await tg.link_channel_from_forward(message)
        if linked:
            await clear_state(user_id)
            await tg.send_text(int(message.chat.id), f"<b>STORAGE CHANNEL LINKED</b>\n\n{html.escape(detail)}")
        else:
            await tg.send_text(int(message.chat.id), f"<b>Channel link not completed.</b>\n{html.escape(detail)}")
        return
    member = await resolve_member_user(message.from_user)
    if not member:
        await welcome(int(message.chat.id), None)
        return
    await create_new_draft(message, member)


async def on_private_text(message: Message) -> None:
    user_id = str(message.from_user.id)
    state = await pop_state(user_id)
    if not state:
        member = await resolve_member_user(message.from_user)
        if member:
            await tg.send_text(int(message.chat.id), "Use the buttons above or send a subtitle file. Type <code>/help</code> for the team menu.")
        else:
            await welcome(int(message.chat.id), None)
        return

    payload = state.get("payload") or {}
    text = (message.text or "").strip()
    if state.get("kind") == "channel_connect":
        linked, detail = await tg.link_channel_from_forward(message)
        if linked:
            await tg.send_text(int(message.chat.id), f"<b>STORAGE CHANNEL LINKED</b>\n\n{html.escape(detail)}")
        else:
            await save_state(user_id, "channel_connect", payload)
            await tg.send_text(int(message.chat.id), f"<b>Channel link not completed.</b>\n{html.escape(detail)}")
        return
    if state.get("kind") == "draft_query":
        draft = await draft_for(payload.get("draft_key", ""), user_id)
        if not draft:
            await tg.send_text(int(message.chat.id), "That submission expired. Send the subtitle file again.")
            return
        clean = re.sub(r"\s+", " ", text)[:120]
        if len(clean) < 2:
            await tg.send_text(int(message.chat.id), "Send at least two characters for the title search.")
            await save_state(user_id, "draft_query", payload)
            return
        await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": {"title_query": clean, "selected_tmdb": None, "updated_at": utcnow()}})
        draft = await get_db().bot_drafts.find_one({"_id": draft["_id"]})
        await show_tmdb_results(draft, "all")
        return
    if state.get("kind") == "draft_note":
        draft = await draft_for(payload.get("draft_key", ""), user_id)
        if not draft:
            await tg.send_text(int(message.chat.id), "That submission expired. Send the subtitle file again.")
            return
        note = re.sub(r"\s+", " ", text)[:800]
        if note in {"-", "skip", "Skip"}:
            note = ""
        await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": {"note": note, "updated_at": utcnow()}})
        await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}))
        return
    if state.get("kind") == "draft_title_note":
        draft = await draft_for(payload.get("draft_key", ""), user_id)
        if not draft:
            await tg.send_text(int(message.chat.id), "That submission expired. Send the subtitle file again.")
            return
        if (draft.get("selected_tmdb") or {}).get("tmdb_type") != "movie":
            await tg.send_text(int(message.chat.id), "Website card notes are available for movies only. Add a note from the individual series episode instead.")
            return
        raw_note = text.strip()
        note_html = "" if raw_note.lower() in {"-", "skip"} else title_page_note_html(raw_note)
        if raw_note and not note_html:
            await save_state(user_id, "draft_title_note", payload)
            await tg.send_text(int(message.chat.id), "That note had no supported text, image or link. Send normal text or safe HTML, or use <code>-</code> to clear it.")
            return
        await get_db().bot_drafts.update_one(
            {"_id": draft["_id"]},
            {"$set": {"title_note_html": note_html, "title_note_touched": True, "updated_at": utcnow()}},
        )
        await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}))
        return
    if state.get("kind") == "draft_episode_note":
        draft = await draft_for(payload.get("draft_key", ""), user_id)
        if not draft:
            await tg.send_text(int(message.chat.id), "That submission expired. Send the subtitle file again.")
            return
        raw_note = text.strip()
        note_html = "" if raw_note.lower() in {"-", "skip"} else episode_page_note_html(raw_note)
        if raw_note and not note_html:
            await save_state(user_id, "draft_episode_note", payload)
            await tg.send_text(int(message.chat.id), "That episode note had no supported text, image or link. Send normal text or safe HTML, or use <code>-</code> to clear it.")
            return
        await get_db().bot_drafts.update_one(
            {"_id": draft["_id"]},
            {"$set": {"episode_note": note_html, "episode_note_touched": True, "updated_at": utcnow()}},
        )
        await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}))
        return
    if state.get("kind") == "ad_code":
        owner = await resolve_member_user(message.from_user)
        if not owner or owner.get("role") != "owner":
            await tg.send_text(int(message.chat.id), "Owner access is required.")
            return
        slot = str(payload.get("slot") or "")
        if slot not in {"site_bar_top", "site_bar_bottom", "home_top", "browse_inline", "title_inline"}:
            await tg.send_text(int(message.chat.id), "That ad placement is unavailable.")
            return
        code = text.strip()
        enabled = code not in {"", "-", "off", "OFF"}
        await get_db().settings.update_one(
            {"type": "ad", "slot": slot},
            {"$set": {"type": "ad", "slot": slot, "code": code if enabled else "", "enabled": enabled, "updated_at": utcnow()}},
            upsert=True,
        )
        await tg.send_text(int(message.chat.id), f"<b>{'Enabled' if enabled else 'Disabled'}</b> the {html.escape(slot)} ad placement.")
        return
    if state.get("kind") == "member_custom_name":
        actor = await resolve_member_user(message.from_user)
        target_id = str(payload.get("target_id") or "")
        target = await get_db().members.find_one({"_id": target_id})
        if not actor or not target:
            await tg.send_text(int(message.chat.id), "That member record is no longer available.")
            return
        if actor.get("_id") != target.get("_id") and actor.get("role") != "owner":
            await tg.send_text(int(message.chat.id), "You can change only your own custom name.")
            return
        raw_name = text.strip()
        custom_name = "" if raw_name.lower() in {"-", "skip"} else member_custom_name(raw_name)
        if raw_name and not custom_name:
            await save_state(user_id, "member_custom_name", payload)
            await tg.send_text(int(message.chat.id), "Send a visible name up to 42 characters, or <code>-</code> to clear it.")
            return
        updated = await set_member_custom_name(target, custom_name)
        if custom_name:
            await tg.send_text(int(message.chat.id), f"Custom name saved: <b>{html.escape(member_display(updated))}</b>. Existing subtitle credits were updated too.")
        else:
            await tg.send_text(int(message.chat.id), "Custom name removed. Your Telegram username is now shown on subtitle credits.")
        return
    if state.get("kind") == "member_username":
        owner = await resolve_member_user(message.from_user)
        if not owner or owner.get("role") != "owner":
            await tg.send_text(int(message.chat.id), "Owner access is required.")
            return
        username = clean_username(text)
        if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
            await tg.send_text(int(message.chat.id), "Send a valid public Telegram username, for example <code>@yourname</code>.")
            await save_state(user_id, "member_username", payload)
            return
        await save_state(user_id, "member_role", {"username": username})
        keyboard = InlineKeyboardMarkup(
            [[pick_button("✦ Maker", "m:addrole:maker"), pick_button("🛠 Editor", "m:addrole:editor")], [pick_button("👑 Owner", "m:addrole:owner"), pick_button("✖ Cancel", "m:cancel")]]
        )
        await tg.send_text(int(message.chat.id), f"Choose access for <b>@{html.escape(username)}</b>.", keyboard)


async def on_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    member = await resolve_member_user(callback.from_user)
    user_id = str(callback.from_user.id)
    chat_id = int(callback.message.chat.id)

    if data.startswith("h:"):
        if not member:
            await tg.answer(callback, "Member access required.", alert=True)
            return
        action = data.split(":", 1)[1]
        await tg.answer(callback)
        if action == "submit":
            await tg.send_text(chat_id, "Send your subtitle file now. Supported: <code>.srt .ass .ssa .vtt .zip</code>")
        elif action == "myfiles":
            await show_my_files(chat_id, member)
        elif action == "myname":
            await save_state(user_id, "member_custom_name", {"target_id": member["_id"]})
            await tg.send_text(
                chat_id,
                "Send the custom name you want shown on your subtitle files. Use <code>-</code> to return to your Telegram username.",
            )
        elif action == "library":
            if member.get("role") in {"owner", "editor"}:
                await show_my_files(chat_id, member, all_files=True)
            else:
                await tg.send_text(chat_id, "The team library is limited to editors and owners.")
        elif action == "members":
            if member.get("role") == "owner":
                await show_members(chat_id)
            else:
                await tg.send_text(chat_id, "Only owners can manage members.")
        elif action == "reports":
            if member.get("role") in {"owner", "editor"}:
                await show_reports(chat_id)
            else:
                await tg.send_text(chat_id, "Only editors and owners can review reports.")
        elif action == "ads":
            if member.get("role") == "owner":
                await show_ads(chat_id)
            else:
                await tg.send_text(chat_id, "Only owners can manage website ads.")
        elif action == "connectchannel":
            if member.get("role") == "owner":
                await begin_channel_link(chat_id, user_id)
            else:
                await tg.send_text(chat_id, "Only owners can link the subtitle storage channel.")
        return

    if data.startswith("f:"):
        if not member:
            await tg.answer(callback, "Member access required.", alert=True)
            return
        parts = data.split(":")
        # Backward compatibility with cards created before the file-management update.
        if len(parts) == 2:
            await open_subtitle_editor(callback, member, parts[1])
            return
        action = parts[1] if len(parts) > 1 else ""
        subtitle_id = parts[2] if len(parts) > 2 else ""
        scope = parts[3] if len(parts) > 3 and parts[3] in {"mine", "team"} else "mine"
        if action == "list":
            scope = parts[2] if len(parts) > 2 and parts[2] in {"mine", "team"} else "mine"
            team = scope == "team" and member.get("role") in {"owner", "editor"}
            await tg.answer(callback)
            await show_my_files(chat_id, member, all_files=team, previous_message_id=callback.message.id)
        elif action == "view" and subtitle_id:
            await show_subtitle_file_card(callback, member, subtitle_id, scope)
        elif action == "edit" and subtitle_id:
            await open_subtitle_editor(callback, member, subtitle_id)
        elif action == "refresh" and subtitle_id:
            await refresh_existing_channel_button(callback, member, subtitle_id, scope)
        elif action == "delete" and subtitle_id:
            await show_subtitle_delete_confirmation(callback, member, subtitle_id, scope)
        elif action == "confirmdelete" and subtitle_id:
            await delete_existing_subtitle(callback, member, subtitle_id, scope)
        else:
            await tg.answer(callback, "Invalid file action.", alert=True)
        return

    if data.startswith("d:"):
        if not member:
            await tg.answer(callback, "Member access required.", alert=True)
            return
        parts = data.split(":")
        if len(parts) < 3:
            await tg.answer(callback, "Invalid action.", alert=True)
            return
        draft = await draft_for(parts[1], user_id)
        if not draft:
            await tg.answer(callback, "This submission expired. Send the file again.", alert=True)
            return
        action = parts[2]
        if action == "cancel":
            await get_db().bot_drafts.delete_one({"_id": draft["_id"]})
            await clear_state(user_id)
            await tg.answer(callback, "Cancelled.")
            await tg.replace_card(chat_id, "<b>SUBMISSION CANCELLED</b>\n\nSend another subtitle file whenever you are ready.", previous_message_id=callback.message.id)
            return
        if action == "query":
            await save_state(user_id, "draft_query", {"draft_key": draft["draft_key"]})
            await tg.answer(callback)
            await tg.send_text(chat_id, "Send the movie or series name to search on TMDB. Example: <code>We Live in Time</code>")
            return
        if action == "search" and len(parts) == 4:
            kind = parts[3] if parts[3] in {"all", "movie", "tv"} else "all"
            await tg.answer(callback, "Searching TMDB…")
            await show_tmdb_results(draft, kind, callback.message.id)
            return
        if action == "pick" and len(parts) == 5:
            kind, raw_id = parts[3], parts[4]
            if kind not in {"movie", "tv"} or not raw_id.isdigit():
                await tg.answer(callback, "Invalid TMDB result.", alert=True)
                return
            if draft_is_series(draft) and kind != "tv":
                await tg.answer(callback, "This filename has an episode number. Choose the TV series result instead.", alert=True)
                return
            try:
                selected = await tmdb_details(kind, int(raw_id))
            except TMDBError as error:
                await tg.answer(callback, "TMDB could not load that title.", alert=True)
                await tg.send_text(chat_id, html.escape(str(error)))
                return
            updates: dict[str, Any] = {"selected_tmdb": selected, "updated_at": utcnow()}
            if selected.get("tmdb_type") == "movie" and not draft.get("title_note_touched"):
                stored_title = await get_db().titles.find_one(
                    {"_id": f"{selected['tmdb_type']}_{selected['tmdb_id']}"},
                    {"page_note_html": 1},
                )
                updates["title_note_html"] = title_page_note_html((stored_title or {}).get("page_note_html"))
            elif selected.get("tmdb_type") == "tv":
                updates["title_note_html"] = ""
                updates["title_note_touched"] = False
            await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": updates})
            await tg.answer(callback, "Title selected.")
            await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}), callback.message.id)
            return
        if action == "retitle":
            await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": {"selected_tmdb": None, "updated_at": utcnow()}})
            await tg.answer(callback)
            await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}), callback.message.id)
            return
        if action == "src" and len(parts) == 4:
            choices = list(SOURCE_TYPES)
            try:
                value = choices[int(parts[3])]
            except (ValueError, IndexError):
                await tg.answer(callback, "Invalid source.", alert=True)
                return
            await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": {"source_type": value, "updated_at": utcnow()}})
            await tg.answer(callback, f"Source: {value}")
            await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}), callback.message.id)
            return
        if action == "res" and len(parts) == 4:
            choices = list(RESOLUTIONS)
            try:
                value = choices[int(parts[3])]
            except (ValueError, IndexError):
                await tg.answer(callback, "Invalid resolution.", alert=True)
                return
            await get_db().bot_drafts.update_one({"_id": draft["_id"]}, {"$set": {"resolution": value, "updated_at": utcnow()}})
            await tg.answer(callback, f"Resolution: {value}")
            await show_draft(await get_db().bot_drafts.find_one({"_id": draft["_id"]}), callback.message.id)
            return
        if action == "note":
            await save_state(user_id, "draft_note", {"draft_key": draft["draft_key"]})
            await tg.answer(callback)
            await tg.send_text(chat_id, "Send the creator note now. Send <code>-</code> to remove the note.")
            return
        if action == "pagenote":
            if (draft.get("selected_tmdb") or {}).get("tmdb_type") != "movie":
                await tg.answer(callback, "Series notes belong to the individual episode pages.", alert=True)
                return
            await save_state(user_id, "draft_title_note", {"draft_key": draft["draft_key"]})
            await tg.answer(callback)
            await tg.send_text(
                chat_id,
                "<b>MOVIE CARD NOTE</b>\n\n"
                "Send the optional rich note for this movie page. It appears after the movie card and before the existing subtitle file list.\n\n"
                "Text: <code>&lt;h2&gt;Release details&lt;/h2&gt;&lt;p&gt;Your note&lt;/p&gt;</code>\n"
                "Image: <code>&lt;img src=\"https://example.com/image.jpg\" alt=\"Image description\"&gt;</code>\n\n"
                "Headings, paragraphs, bold text, lists, links, images, tables and details are supported. Only secure <code>https://</code> links and images are kept. Send <code>-</code> to remove the note.",
            )
            return
        if action == "epnote":
            if not (draft_is_series(draft) and draft.get("season") and draft.get("episode")):
                await tg.answer(callback, "Episode details were not detected for this file.", alert=True)
                return
            await save_state(user_id, "draft_episode_note", {"draft_key": draft["draft_key"]})
            await tg.answer(callback)
            await tg.send_text(
                chat_id,
                "<b>EPISODE PAGE NOTE</b>\n\n"
                "Send an optional Sinhala rich note for this one episode. It appears below the TMDB synopsis and before the subtitle files.\n\n"
                "Text: <code>&lt;h2&gt;වැදගත් සටහන&lt;/h2&gt;&lt;p&gt;ඔබගේ විස්තරය&lt;/p&gt;</code>\n"
                "Image: <code>&lt;img src=\"https://example.com/image.jpg\" alt=\"Episode image\"&gt;</code>\n\n"
                "Headings, paragraphs, bold text, lists, links, images, tables and details are supported. Only secure <code>https://</code> links and images are kept. Send <code>-</code> to remove the note.",
            )
            return
        if action == "publish":
            await tg.answer(callback, "Saving…")
            if draft.get("mode") == "edit":
                await save_edit_draft(draft, member, callback)
            else:
                await publish_new_draft(draft, member, callback)
            return
        await tg.answer(callback, "Unknown action.", alert=True)
        return

    if data.startswith("m:"):
        if not member or member.get("role") != "owner":
            await tg.answer(callback, "Owner access required.", alert=True)
            return
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "add":
            await save_state(user_id, "member_username", {})
            await tg.answer(callback)
            await tg.send_text(chat_id, "Send the new member's public Telegram username, for example <code>@subtitlemaker</code>.")
        elif action == "addrole" and len(parts) == 3:
            state = await pop_state(user_id)
            username = ((state or {}).get("payload") or {}).get("username")
            if not username:
                await tg.answer(callback, "Start again with Add member.", alert=True)
                return
            try:
                added = await add_member_from_username(username, parts[2])
            except ValueError as error:
                await tg.answer(callback, str(error), alert=True)
                return
            await tg.answer(callback, "Member added.")
            await tg.send_text(chat_id, f"<b>@{html.escape(added['username'])}</b> is now a <b>{role_label(added['role'])}</b>. They must press <code>/start</code> in this bot once to link their account.")
        elif action == "list":
            await tg.answer(callback)
            await show_members(chat_id, callback.message.id)
        elif action == "view" and len(parts) == 3:
            await tg.answer(callback)
            await show_member_card(callback, parts[2])
        elif action == "name" and len(parts) == 3:
            target = await get_db().members.find_one({"_id": parts[2]})
            if not target:
                await tg.answer(callback, "Member not found.", alert=True)
                return
            await save_state(user_id, "member_custom_name", {"target_id": target["_id"]})
            await tg.answer(callback)
            await tg.send_text(
                chat_id,
                f"Send the custom public name for <b>{html.escape(member_display(target))}</b>. Use <code>-</code> to clear it.",
            )
        elif action == "set" and len(parts) == 4:
            target = await get_db().members.find_one({"_id": parts[2]})
            role = parts[3]
            if not target or role not in {"owner", "editor", "maker"}:
                await tg.answer(callback, "Invalid member action.", alert=True)
                return
            if target.get("telegram_id") == str(settings.owner_id):
                await tg.answer(callback, "The configured owner role cannot be changed.", alert=True)
                return
            await get_db().members.update_one({"_id": target["_id"]}, {"$set": {"role": role, "active": True, "updated_at": utcnow()}})
            await tg.answer(callback, "Role updated.")
            await tg.replace_card(
                chat_id,
                f"Updated <b>{html.escape(member_display(target))}</b> to <b>{role_label(role)}</b>.",
                InlineKeyboardMarkup([[pick_button("← Members", "m:list")]]),
                previous_message_id=callback.message.id,
            )
        elif action == "toggle" and len(parts) == 3:
            target = await get_db().members.find_one({"_id": parts[2]})
            if not target:
                await tg.answer(callback, "Member not found.", alert=True)
                return
            if target.get("telegram_id") == str(settings.owner_id):
                await tg.answer(callback, "The configured owner cannot be deactivated.", alert=True)
                return
            active = not bool(target.get("active"))
            await get_db().members.update_one({"_id": target["_id"]}, {"$set": {"active": active, "updated_at": utcnow()}})
            await tg.answer(callback, "Access updated.")
            await tg.replace_card(
                chat_id,
                f"<b>{html.escape(member_display(target))}</b> is now <b>{'active' if active else 'inactive'}</b>.",
                InlineKeyboardMarkup([[pick_button("← Members", "m:list")]]),
                previous_message_id=callback.message.id,
            )
        elif action == "remove" and len(parts) == 3:
            await tg.answer(callback)
            await show_member_remove_confirmation(callback, parts[2])
        elif action == "confirmremove" and len(parts) == 3:
            target = await get_db().members.find_one({"_id": parts[2]})
            if not target:
                await tg.answer(callback, "Member not found.", alert=True)
                return
            if target.get("telegram_id") == str(settings.owner_id):
                await tg.answer(callback, "The configured owner cannot be removed.", alert=True)
                return
            await get_db().bot_drafts.delete_many({"member_id": target["_id"]})
            if target.get("telegram_id"):
                await get_db().bot_states.delete_many({"user_id": str(target["telegram_id"])})
            await get_db().members.delete_one({"_id": target["_id"]})
            await tg.answer(callback, "Member removed.")
            await tg.replace_card(
                chat_id,
                f"<b>MEMBER REMOVED</b>\n\n🗑 {html.escape(member_display(target))}\n\nTheir subtitle files were kept, but they can no longer access the bot.",
                InlineKeyboardMarkup([[pick_button("← Members", "m:list")]]),
                previous_message_id=callback.message.id,
            )
        elif action == "cancel":
            await clear_state(user_id)
            await tg.answer(callback, "Cancelled.")
        return

    if data.startswith("a:"):
        if not member or member.get("role") != "owner":
            await tg.answer(callback, "Owner access required.", alert=True)
            return
        parts = data.split(":")
        valid_slots = {"site_bar_top", "site_bar_bottom", "home_top", "browse_inline", "title_inline"}
        if len(parts) != 3 or parts[2] not in valid_slots:
            await tg.answer(callback, "Invalid ad placement.", alert=True)
            return
        if parts[1] == "slot":
            await save_state(user_id, "ad_code", {"slot": parts[2]})
            await tg.answer(callback)
            if parts[2] in {"site_bar_top", "site_bar_bottom"}:
                position = "above the top navigation" if parts[2] == "site_bar_top" else "directly below the top navigation"
                prompt = (
                    "Send the ad HTML now. It scrolls as a small animated bar "
                    f"{position}. Keep it short (a line of text, a small logo, or a compact link). "
                    "Send <code>-</code> to disable this placement."
                )
            else:
                prompt = "Send the full ad-network HTML/code now. It shows as a boxed banner on this placement's page. Send <code>-</code> to disable this placement."
            await tg.send_text(chat_id, prompt)
        elif parts[1] == "off":
            await get_db().settings.update_one(
                {"type": "ad", "slot": parts[2]},
                {"$set": {"type": "ad", "slot": parts[2], "code": "", "enabled": False, "updated_at": utcnow()}},
                upsert=True,
            )
            await tg.answer(callback, "Ad disabled.")
            await tg.send_text(chat_id, f"Disabled the <b>{html.escape(parts[2])}</b> ad placement.")
        return

    if data.startswith("r:"):
        if not member or member.get("role") not in {"owner", "editor"}:
            await tg.answer(callback, "Editor or owner access required.", alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1] == "close":
            await get_db().reports.update_one({"_id": parts[2]}, {"$set": {"status": "closed", "closed_by": member["_id"], "closed_at": utcnow()}})
            await tg.answer(callback, "Report closed.")
        return

    await tg.answer(callback, "Unknown action.")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI public website — server-rendered Jinja pages
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialise_database()
    try:
        await tg.start(on_channel_message, on_private_document, on_private_command, on_private_text, on_callback)
        logger.info("Telegram client online as @%s", tg.bot_username or "bot")
    except Exception:
        logger.exception("Telegram client could not start")
    yield
    await tg.stop()
    await close_database()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=True), name="static")


def public_cast(raw_cast: Any) -> list[dict[str, Any]]:
    """Normalise stored cast entries into ``{name, character, photo}``.

    Titles saved before cast photos were tracked stored plain name strings;
    keep reading those correctly alongside the newer photo-aware records.
    """
    people = []
    for person in list(raw_cast or [])[:12]:
        if isinstance(person, dict):
            name = person.get("name")
            if not name:
                continue
            people.append({"name": name, "character": person.get("character") or "", "photo": person.get("photo_url") or ""})
        elif person:
            people.append({"name": str(person), "character": "", "photo": ""})
    return people


def public_title(title: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": title.get("_id", ""),
        "tmdbId": title.get("tmdb_id"),
        "type": title.get("tmdb_type", "unknown"),
        "label": title.get("media_label") or ("Series" if title.get("tmdb_type") == "tv" else "Movie"),
        "name": title.get("name") or "Untitled",
        "year": title.get("release_year"),
        "poster": title.get("poster_url") or "",
        "backdrop": tmdb_image_size(title.get("backdrop_url"), "w1280"),
        "rating": float(title.get("rating") or 0),
        "overview": title.get("overview") or "",
        "cast": public_cast(title.get("cast")),
        "subtitleCount": int(title.get("_sinhala_subtitle_count") or title.get("subtitle_count") or 0),
        "downloadCount": int(title.get("download_count") or 0),
        "updatedAt": iso(title.get("updated_at")),
    }


def public_subtitle(subtitle: dict[str, Any]) -> dict[str, Any]:
    guess = parse_subtitle_name(str(subtitle.get("filename") or "subtitle.srt"))
    source = subtitle.get("source_type") or guess.source_type
    resolution = subtitle.get("resolution") or guess.resolution
    codec = subtitle.get("codec") or guess.codec
    bit_depth = subtitle.get("bit_depth") or guess.bit_depth
    hdr = subtitle.get("hdr") or guess.hdr
    release_group = subtitle.get("release_group") or guess.release_group
    tech = [source, resolution, codec, bit_depth, hdr]
    return {
        "id": subtitle.get("_id", ""),
        "filename": subtitle.get("filename") or "subtitle.srt",
        "size": int(subtitle.get("file_size") or 0),
        "sizeLabel": format_size(int(subtitle.get("file_size") or 0)),
        "language": subtitle.get("language") or "Sinhala",
        "source": source,
        "resolution": resolution,
        "codec": codec,
        "bitDepth": bit_depth,
        "hdr": hdr,
        "releaseGroup": release_group,
        "technicalLabel": " · ".join(item for item in tech if item and item != "Other") or "Other",
        "season": subtitle.get("season"),
        "episode": subtitle.get("episode"),
        "episodeEnd": subtitle.get("episode_end"),
        "note": subtitle.get("note") or "",
        "maker": subtitle.get("uploader_name") or f"{settings.site_name} Team",
        "makerUsername": subtitle.get("uploader_username") or "",
        "downloads": int(subtitle.get("download_count") or 0),
        "createdAt": iso(subtitle.get("created_at")),
        "downloadUrl": f"/download/{subtitle.get('_id', '')}",
    }


def public_comment(comment: dict[str, Any], like_count: int = 0, liked: bool = False) -> dict[str, Any]:
    return {
        "id": comment.get("_id", ""),
        "name": comment.get("display_name") or "Subtitle viewer",
        "body": comment.get("body") or "",
        "createdAt": iso(comment.get("created_at")),
        "likes": int(like_count),
        "liked": bool(liked),
    }


async def title_reactions(title_id: str, request: Request) -> dict[str, Any]:
    database = get_db()
    summary = await database.votes.aggregate(
        [
            {"$match": {"title_id": title_id}},
            {"$group": {"_id": "$value", "count": {"$sum": 1}}},
        ]
    ).to_list(length=4)
    counts = {int(item["_id"]): int(item["count"]) for item in summary}
    mine = await database.votes.find_one({"title_id": title_id, "visitor_id": visitor_id(request)})
    return {"likes": counts.get(1, 0), "dislikes": counts.get(-1, 0), "value": int((mine or {}).get("value") or 0)}


async def title_comments(title_id: str, request: Request, limit: int = 100) -> list[dict[str, Any]]:
    database = get_db()
    comments = await database.comments.find({"title_id": title_id, "status": "visible"}).sort("created_at", -1).to_list(length=limit)
    ids = [row["_id"] for row in comments]
    if not ids:
        return []
    counted = await database.comment_votes.aggregate(
        [
            {"$match": {"comment_id": {"$in": ids}}},
            {"$group": {"_id": "$comment_id", "count": {"$sum": 1}}},
        ]
    ).to_list(length=limit)
    totals = {str(row["_id"]): int(row["count"]) for row in counted}
    mine = await database.comment_votes.find({"comment_id": {"$in": ids}, "visitor_id": visitor_id(request)}).to_list(length=limit)
    mine_ids = {str(row["comment_id"]) for row in mine}
    return [public_comment(row, totals.get(str(row["_id"]), 0), str(row["_id"]) in mine_ids) for row in comments]


@app.get("/api/public/site")
async def api_site() -> JSONResponse:
    return JSONResponse(
        {
            "name": APP_NAME,
            "siteName": settings.site_name,
            "botUsername": tg.bot_username,
            "ads": await active_ads(),
        }
    )


@app.get("/api/public/home")
async def api_home() -> JSONResponse:
    cards = await title_cards(limit=12)
    return JSONResponse({"stats": await public_stats(), "titles": [public_title(item) for item in cards], "ads": await active_ads()})


@app.get("/api/public/titles")
async def api_titles(q: str = "", kind: str = "all", page: int = 1) -> JSONResponse:
    safe_kind = kind if kind in {"all", "movie", "tv"} else "all"
    safe_page = max(1, min(int(page or 1), 40))
    cards = await title_cards(q[:100], safe_kind, limit=360)
    size = 30
    start = (safe_page - 1) * size
    items = cards[start : start + size]
    return JSONResponse(
        {
            "items": [public_title(item) for item in items],
            "total": len(cards),
            "page": safe_page,
            "pages": max(1, (len(cards) + size - 1) // size),
        }
    )


@app.get("/api/public/titles/{title_id}")
async def api_title(request: Request, title_id: str) -> JSONResponse:
    database = get_db()
    title = await database.titles.find_one({"_id": title_id, "status": "published"})
    if not title:
        raise HTTPException(status_code=404, detail="This title is unavailable.")
    subtitles = await database.subtitles.find({"title_id": title_id, "status": "published", "language": "Sinhala"}).sort("created_at", -1).to_list(length=600)
    if not subtitles:
        raise HTTPException(status_code=404, detail="This title has no published Sinhala subtitles.")
    related = await title_cards(media_type=str(title.get("tmdb_type") or "all"), limit=20)
    related = [item for item in related if item.get("_id") != title_id][:8]
    episode_index = title_episode_groups(subtitles) if title.get("tmdb_type") == "tv" else []
    season_releases = [item for item in subtitles if not subtitle_episode_targets(item)]
    payload = public_title({**title, "_sinhala_subtitle_count": len(subtitles)})
    payload.update(
        {
            # Keep the complete list for compatibility, while the new TV view
            # sends viewers into a focused page for every SxxExx release.
            "subtitles": [public_subtitle(item) for item in subtitles],
            "pageNoteHtml": title_page_note_html(title.get("page_note_html")) if title.get("tmdb_type") == "movie" else "",
            "episodes": episode_index,
            "seasonReleases": [public_subtitle(item) for item in season_releases],
            "reactions": await title_reactions(title_id, request),
            "commentCount": await database.comments.count_documents({"title_id": title_id, "status": "visible"}),
            "related": [public_title(item) for item in related],
            "ads": await active_ads(),
        }
    )
    return JSONResponse(payload)


@app.get("/api/public/titles/{title_id}/episodes/{season}/{episode}")
async def api_episode(title_id: str, season: int, episode: int) -> JSONResponse:
    if not (1 <= season <= 300 and 1 <= episode <= 999):
        raise HTTPException(status_code=404, detail="This episode link is invalid.")
    database = get_db()
    title = await database.titles.find_one({"_id": title_id, "status": "published", "tmdb_type": "tv"})
    if not title:
        raise HTTPException(status_code=404, detail="This series is unavailable.")
    all_subtitles = await database.subtitles.find(
        {"title_id": title_id, "status": "published", "language": "Sinhala"}
    ).sort("created_at", -1).to_list(length=600)
    subtitles = [item for item in all_subtitles if subtitle_matches_episode(item, season, episode)]
    if not subtitles:
        raise HTTPException(status_code=404, detail="No published Sinhala subtitle is available for this episode.")
    payload = public_title({**title, "_sinhala_subtitle_count": len(all_subtitles)})
    payload.update(
        {
            "episode": await episode_page_data(title, season, episode),
            "subtitles": [public_subtitle(item) for item in subtitles],
            "episodes": title_episode_groups(all_subtitles),
            "ads": await active_ads(),
        }
    )
    return JSONResponse(payload)


@app.get("/api/public/titles/{title_id}/comments")
async def api_comments(request: Request, title_id: str) -> JSONResponse:
    exists = await get_db().titles.find_one({"_id": title_id, "status": "published"}, {"_id": 1})
    if not exists:
        raise HTTPException(status_code=404, detail="This title is unavailable.")
    return JSONResponse({"items": await title_comments(title_id, request)})


@app.post("/api/public/titles/{title_id}/reaction")
async def api_react_title(request: Request, title_id: str) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    value = int(body.get("value") or 0)
    if value not in {-1, 1}:
        raise HTTPException(status_code=422, detail="Choose like or dislike.")
    database = get_db()
    if not await database.titles.find_one({"_id": title_id, "status": "published"}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="This title is unavailable.")
    lookup = {"title_id": title_id, "visitor_id": visitor_id(request)}
    existing = await database.votes.find_one(lookup)
    if existing and int(existing.get("value") or 0) == value:
        await database.votes.delete_one({"_id": existing["_id"]})
    else:
        await database.votes.update_one(lookup, {"$set": {**lookup, "value": value, "updated_at": utcnow()}}, upsert=True)
    return JSONResponse(await title_reactions(title_id, request))


@app.post("/api/public/titles/{title_id}/comments")
async def api_create_comment(request: Request, title_id: str) -> JSONResponse:
    try:
        data = await request.json()
    except ValueError:
        data = {}
    database = get_db()
    if not await database.titles.find_one({"_id": title_id, "status": "published"}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="This title is unavailable.")
    text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()[:800]
    name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()[:42]
    if len(text) < 2:
        raise HTTPException(status_code=422, detail="Write at least two characters before posting a comment.")
    now = utcnow()
    last = request.session.get("last_comment_at")
    if last:
        try:
            if now.timestamp() - float(last) < 12:
                raise HTTPException(status_code=429, detail="Please wait a few seconds before posting another comment.")
        except ValueError:
            pass
    record = {
        "_id": new_id("comment_"),
        "title_id": title_id,
        "visitor_id": visitor_id(request),
        "display_name": name or "Subtitle viewer",
        "body": text,
        "status": "visible",
        "created_at": now,
        "updated_at": now,
    }
    await database.comments.insert_one(record)
    request.session["last_comment_at"] = str(now.timestamp())
    return JSONResponse({"item": public_comment(record)}, status_code=201)


@app.post("/api/public/comments/{comment_id}/reaction")
async def api_react_comment(request: Request, comment_id: str) -> JSONResponse:
    database = get_db()
    comment = await database.comments.find_one({"_id": comment_id, "status": "visible"})
    if not comment:
        raise HTTPException(status_code=404, detail="This comment is unavailable.")
    lookup = {"comment_id": comment_id, "visitor_id": visitor_id(request)}
    existing = await database.comment_votes.find_one(lookup)
    if existing:
        await database.comment_votes.delete_one({"_id": existing["_id"]})
        liked = False
    else:
        await database.comment_votes.insert_one({"_id": new_id("comment_vote_"), **lookup, "created_at": utcnow()})
        liked = True
    total = await database.comment_votes.count_documents({"comment_id": comment_id})
    return JSONResponse({"liked": liked, "likes": total})


@app.post("/api/public/subtitles/{subtitle_id}/report")
async def api_report_subtitle(request: Request, subtitle_id: str) -> JSONResponse:
    try:
        data = await request.json()
    except ValueError:
        data = {}
    reason = re.sub(r"\s+", " ", str(data.get("reason") or "")).strip()[:500]
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id, "status": "published", "language": "Sinhala"}, {"title_id": 1})
    if not subtitle or not reason:
        raise HTTPException(status_code=422, detail="Add a short reason for the report.")
    await get_db().reports.insert_one(
        {
            "_id": new_id("report_"),
            "subtitle_id": subtitle_id,
            "reason": reason,
            "status": "open",
            "created_at": utcnow(),
        }
    )
    return JSONResponse({"ok": True})


@app.get("/download/{subtitle_id}")
async def download_subtitle(subtitle_id: str) -> Any:
    subtitle = await get_db().subtitles.find_one({"_id": subtitle_id, "status": "published", "language": "Sinhala"})
    if not subtitle:
        raise HTTPException(status_code=404, detail="That subtitle file is unavailable.")
    try:
        payload: BytesIO = await tg.download_document(file_id=str(subtitle.get("file_id") or ""), message_id=int(subtitle.get("message_id") or 0) or None)
    except TelegramStorageError as error:
        logger.warning("Storage failure for %s: %s", subtitle_id, error)
        await get_db().subtitles.update_one({"_id": subtitle_id}, {"$set": {"last_storage_error": str(error)[:700], "storage_checked_at": utcnow()}})
        raise HTTPException(status_code=503, detail="Subtitle storage is temporarily unavailable. Please retry.") from error
    except Exception as error:
        logger.exception("Unexpected download failure for %s", subtitle_id)
        raise HTTPException(status_code=503, detail="The file could not be prepared. Please retry.") from error
    await get_db().subtitles.update_one({"_id": subtitle_id}, {"$inc": {"download_count": 1}, "$set": {"updated_at": utcnow(), "last_storage_error": "", "storage_checked_at": utcnow()}})
    await refresh_title_counts(subtitle["title_id"])
    safe_name = quote(str(subtitle.get("filename") or "subtitle.srt"))
    filename = str(subtitle.get("filename") or "").lower()
    content_type = "application/zip" if filename.endswith(".zip") else "application/x-subrip"
    return StreamingResponse(payload, media_type=content_type, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}", "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


async def template_context(request: Request, page_name: str, **values: Any) -> dict[str, Any]:
    """Shared Jinja context for the public, server-rendered website."""
    return {
        "request": request,
        "page_name": page_name,
        "query": str(request.query_params.get("q") or ""),
        "brand_version": brand_icon_version(),
        "asset_version": site_asset_version(),
        "site_name": settings.site_name,
        "current_year": datetime.now().year,
        "ads": await active_ads(),
        **values,
    }


async def error_page(request: Request, status_code: int, heading: str, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context=await template_context(request, "error", code=status_code, heading=heading, message=message),
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse, name="website_home")
async def website_home(request: Request) -> HTMLResponse:
    """Render the compact English home page with separate movie and series shelves."""
    cards = [public_title(item) for item in await title_cards(limit=30)]
    movies = [item for item in cards if item.get("type") == "movie"]
    series = [item for item in cards if item.get("type") == "tv"]
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=await template_context(
            request,
            "home",
            featured=cards[0] if cards else None,
            latest=cards[:8],
            movies=movies[:12],
            series=series[:12],
            stats=await public_stats(),
        ),
    )


@app.get("/browse", response_class=HTMLResponse, name="website_browse")
async def website_browse(request: Request, q: str = "", kind: str = "all", page: int = 1) -> HTMLResponse:
    safe_kind = kind if kind in {"all", "movie", "tv"} else "all"
    safe_page = max(1, min(int(page or 1), 40))
    cards = [public_title(item) for item in await title_cards(q[:100], safe_kind, limit=360)]
    size = 30
    pages = max(1, (len(cards) + size - 1) // size)
    safe_page = min(safe_page, pages)
    visible = cards[(safe_page - 1) * size : safe_page * size]
    return templates.TemplateResponse(
        request=request,
        name="browse.html",
        context=await template_context(request, "browse", titles=visible, total=len(cards), page=safe_page, pages=pages, kind=safe_kind, query=q[:100]),
    )


async def public_title_page_context(request: Request, title_id: str) -> dict[str, Any] | None:
    database = get_db()
    title_document = await database.titles.find_one({"_id": title_id, "status": "published"})
    if not title_document:
        return None
    subtitle_documents = await database.subtitles.find(
        {"title_id": title_id, "status": "published", "language": "Sinhala"}
    ).sort("created_at", -1).to_list(length=600)
    if not subtitle_documents:
        return None
    title = public_title({**title_document, "_sinhala_subtitle_count": len(subtitle_documents)})
    is_series = title.get("type") == "tv"
    episodes = title_episode_groups(subtitle_documents) if is_series else []
    season_releases = [public_subtitle(item) for item in subtitle_documents if not subtitle_episode_targets(item)]
    related_docs = await title_cards(media_type=str(title_document.get("tmdb_type") or "all"), limit=20)
    related = [public_title(item) for item in related_docs if item.get("_id") != title_id][:8]
    return {
        "title": title,
        "is_series": bool(is_series and episodes),
        "episodes": episodes,
        "releases": season_releases if is_series else [public_subtitle(item) for item in subtitle_documents],
        "page_note_html": title_page_note_html(title_document.get("page_note_html")) if title_document.get("tmdb_type") == "movie" else "",
        "reactions": await title_reactions(title_id, request),
        "comments": await title_comments(title_id, request),
        "cast": title.get("cast") or [],
        "related": related,
    }


@app.get("/title/{title_id}", response_class=HTMLResponse, name="website_title")
async def website_title(request: Request, title_id: str) -> HTMLResponse:
    context = await public_title_page_context(request, title_id)
    if context is None:
        return await error_page(request, 404, "Title not found", "This title does not have published Sinhala subtitle files.")
    return templates.TemplateResponse(request=request, name="title.html", context=await template_context(request, "title", **context))


@app.get("/title/{title_id}/s{season}e{episode}", response_class=HTMLResponse, name="website_episode")
async def website_episode(request: Request, title_id: str, season: int, episode: int) -> HTMLResponse:
    if not (1 <= season <= 300 and 1 <= episode <= 999):
        return await error_page(request, 404, "Invalid episode link", "Please choose the episode again from the series page.")
    database = get_db()
    title_document = await database.titles.find_one({"_id": title_id, "status": "published", "tmdb_type": "tv"})
    if not title_document:
        return await error_page(request, 404, "Series not found", "This series is currently unavailable.")
    all_subtitles = await database.subtitles.find(
        {"title_id": title_id, "status": "published", "language": "Sinhala"}
    ).sort("created_at", -1).to_list(length=600)
    matching = [item for item in all_subtitles if subtitle_matches_episode(item, season, episode)]
    if not matching:
        return await error_page(request, 404, "Subtitle not found", "There is no published Sinhala subtitle file for this episode yet.")
    title = public_title({**title_document, "_sinhala_subtitle_count": len(all_subtitles)})
    episode_data = await episode_page_data(title_document, season, episode)
    episodes = title_episode_groups(all_subtitles)
    current_index = next(
        (
            index
            for index, item in enumerate(episodes)
            if int(item.get("season") or 0) == season and int(item.get("episode") or 0) == episode
        ),
        -1,
    )
    previous_episode = episodes[current_index - 1] if current_index > 0 else None
    next_episode = episodes[current_index + 1] if 0 <= current_index < len(episodes) - 1 else None
    return templates.TemplateResponse(
        request=request,
        name="episode.html",
        context=await template_context(
            request,
            "episode",
            title=title,
            episode=episode_data,
            backdrop=episode_data.get("still") or title.get("backdrop") or "",
            subtitles=[public_subtitle(item) for item in matching],
            previous_episode=previous_episode,
            next_episode=next_episode,
        ),
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "database": "subtitle", "telegram_online": tg.online, "bot_username": tg.bot_username, "configured": settings.configured, "frontend": TEMPLATE_DIR.exists()})


async def _brand_icon_response() -> FileResponse:
    if BRAND_ICON.exists():
        return FileResponse(BRAND_ICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=31536000, immutable"})
    raise HTTPException(status_code=404, detail="Brand icon unavailable.")


@app.get("/brand.svg", include_in_schema=False)
async def brand_svg() -> Any:
    return await _brand_icon_response()


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg() -> Any:
    return await _brand_icon_response()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> Any:
    return await _brand_icon_response()


@app.get("/apple-touch-icon.svg", include_in_schema=False)
async def apple_touch_icon_svg() -> Any:
    return await _brand_icon_response()


@app.get("/{path:path}", include_in_schema=False)
async def website_not_found(request: Request, path: str) -> HTMLResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found.")
    return await error_page(request, 404, "Page not found", "This link may have moved or is no longer available.")

