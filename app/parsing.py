from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

# The site intentionally handles Sinhala subtitle releases only. Files can arrive
# as normal subtitle files or archive bundles created by subtitle makers.
ALLOWED_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".zip"}
SOURCE_TYPES = (
    "WEB-DL",
    "WEBRip",
    "BluRay",
    "BDRip",
    "HDRip",
    "HDTV",
    "DVDRip",
    "REMUX",
    "CAM",
    "HDTS",
    "TC",
    "Other",
)
RESOLUTIONS = ("2160p", "1440p", "1080p", "720p", "576p", "480p", "360p", "Other")

_LANGUAGE_MARKERS = r"(?:sinhala|sinhalese|සිංහල|\[\s*si\s*\]|\bsi\b|\besub(?:s)?\b)"
_RELEASE_MARKERS = (
    r"\bS\d{1,2}\s*E\d{1,3}(?:\s*(?:-|to|E)\s*E?\d{1,3})?\b",
    r"\b\d{1,2}\s*[xX]\s*\d{1,3}(?:\s*(?:-|to)\s*\d{1,3})?\b",
    r"\b(?:Season|Series)\s*\d{1,2}\b",
    r"\b(?:Episode|Ep)\s*\d{1,3}\b",
    r"\b(?:WEB\s*-?\s*DL|WEBDL|WEB\s*-?\s*RIP|WEBRIP|BLU\s*-?\s*RAY|BDRIP|HDRIP|HDTV|DVDRIP|REMUX|HDCAM|CAMRIP|CAM|HDTS|HQ\s*TS|TELECINE)\b",
    r"\b(?:2160|1440|1080|720|576|540|480|360)p\b",
    r"\b(?:3840\s*[x×]\s*2160|2560\s*[x×]\s*1440|1920\s*[x×]\s*1080|1280\s*[x×]\s*720|854\s*[x×]\s*480)\b",
    r"\b(?:4K|UHD|FHD)\b",
    _LANGUAGE_MARKERS,
    r"\b(?:x265|x264|h\.?265|h\.?264|hevc|avc|av1|xvid|10\s*bit|8\s*bit|12\s*bit|hdr10\+?|dolby\s*vision|dovi|dv)\b",
    r"\b(?:AMZN|NF|NETFLIX|DSNP|DISNEY\+?|ATVP|APPLE\s*TV\+?|HMAX|MAX|HULU|PCOK|PEACOCK|CR|CRUNCHYROLL|JIO|ZEE5|SONYLIV|HOTSTAR|AHA)\b",
    r"\b(?:PROPER|REPACK|EXTENDED|UNRATED|COMPLETE|INTERNAL)\b",
)


@dataclass(frozen=True)
class SubtitleGuess:
    filename: str
    title_guess: str
    source_type: str
    resolution: str
    season: int | None
    episode: int | None
    episode_end: int | None
    language: str
    note: str
    codec: str
    bit_depth: str
    hdr: str
    release_group: str

    @property
    def episode_label(self) -> str:
        if self.season is None and self.episode is None:
            return ""
        if self.season is not None and self.episode is not None:
            label = f"S{self.season:02d}E{self.episode:02d}"
        elif self.season is not None:
            label = f"Season {self.season}"
        else:
            label = f"Episode {self.episode}"
        if self.episode_end is not None and self.season is not None:
            label += f"–E{self.episode_end:02d}"
        return label

    @property
    def technical_label(self) -> str:
        values = [self.source_type, self.resolution, self.codec, self.bit_depth, self.hdr]
        return " · ".join(value for value in values if value and value != "Other") or "Other"


def valid_subtitle_filename(filename: str) -> bool:
    return PurePath(filename or "").suffix.lower() in ALLOWED_EXTENSIONS


def clean_username(value: str | None) -> str:
    return (value or "").strip().lstrip("@").lower()


def _normalise(raw: str) -> str:
    """Turn common release punctuation into searchable text without losing a year."""
    text = PurePath(raw or "subtitle.srt").stem
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"[\u2013\u2014]", "-", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t-_")


def _first_match_position(text: str, patterns: tuple[str, ...]) -> int | None:
    positions: list[int] = []
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            positions.append(match.start())
    return min(positions) if positions else None


def _extract_episode(text: str) -> tuple[int | None, int | None, int | None]:
    """Return season, episode and optional episode end from common release forms."""
    patterns = (
        # S01E01, S01 E01, S01E01-E03, S01E01E03
        r"\bS(?P<s>\d{1,2})\s*E(?P<e>\d{1,3})(?:\s*(?:-|to|E)\s*E?(?P<end>\d{1,3}))?\b",
        # 1x01 and 1x01-03
        r"\b(?P<s>\d{1,2})\s*[xX]\s*(?P<e>\d{1,3})(?:\s*(?:-|to)\s*(?P<end>\d{1,3}))?\b",
        # Season 1 Episode 1 / Season 01 Ep 001
        r"\b(?:Season|Series)\s*(?P<s>\d{1,2})\s*(?:Episode|Ep)\s*(?P<e>\d{1,3})\b",
        # E01 has no reliable season but is still a series hint.
        r"\bE(?:pisode)?\s*(?P<e>\d{1,3})\b",
        r"\b(?:Episode|Ep)\s*(?P<e>\d{1,3})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        groups = match.groupdict()
        season = groups.get("s")
        episode = groups.get("e")
        episode_end = groups.get("end")
        return (
            int(season) if season else None,
            int(episode) if episode else None,
            int(episode_end) if episode_end else None,
        )
    season_match = re.search(r"\b(?:S|Season|Series)\s*(\d{1,2})\b", text, flags=re.I)
    return (int(season_match.group(1)) if season_match else None, None, None)


def find_source(text: str) -> str:
    # Ordering matters: WEB-DL needs to win before generic WEBRip/WEB matches.
    patterns = (
        ("REMUX", r"\b(?:UHD\s*)?REMUX\b"),
        ("BluRay", r"\b(?:BLU\s*-?\s*RAY|BDMV|BD50|BD25|UHD\s*BLURAY)\b"),
        ("BDRip", r"\bBD\s*-?\s*RIP\b"),
        ("WEB-DL", r"\b(?:WEB\s*-?\s*DL|WEBDL|WEB\s*DOWNLOAD|NF\s*WEB|AMZN\s*WEB|DSNP\s*WEB|ATVP\s*WEB|HMAX\s*WEB|HULU\s*WEB|PCOK\s*WEB|CR\s*WEB|JIO\s*WEB|ZEE5\s*WEB|SONYLIV\s*WEB|HOTSTAR\s*WEB|AHA\s*WEB)\b"),
        ("WEBRip", r"\b(?:WEB\s*-?\s*RIP|WEBRIP|WEB\s*CAP|\bWEB\b)\b"),
        ("HDRip", r"\b(?:HD\s*-?\s*RIP|HDRIP)\b"),
        ("HDTV", r"\bHDTV\b"),
        ("DVDRip", r"\bDVD\s*-?\s*RIP\b"),
        ("HDTS", r"\b(?:HDTS|HQ\s*TS)\b"),
        ("TC", r"\b(?:TELECINE|\bTC\b)\b"),
        ("CAM", r"\b(?:HDCAM|CAMRIP|CAM)\b"),
    )
    for label, pattern in patterns:
        if re.search(pattern, text, flags=re.I):
            return label
    return "Other"


def find_resolution(text: str) -> str:
    match = re.search(r"\b(2160|1440|1080|720|576|540|480|360)\s*p\b", text, flags=re.I)
    if match:
        raw = match.group(1)
        return "576p" if raw == "540" else f"{raw}p"
    dimensions = re.search(r"\b(?:3840\s*[x×]\s*2160|2560\s*[x×]\s*1440|1920\s*[x×]\s*1080|1280\s*[x×]\s*720|854\s*[x×]\s*480)\b", text, flags=re.I)
    if dimensions:
        dimensions_text = dimensions.group(0).replace("×", "x").replace(" ", "")
        return {"3840x2160": "2160p", "2560x1440": "1440p", "1920x1080": "1080p", "1280x720": "720p", "854x480": "480p"}.get(dimensions_text, "Other")
    if re.search(r"\b(?:4K|UHD)\b", text, flags=re.I):
        return "2160p"
    if re.search(r"\bFHD\b", text, flags=re.I):
        return "1080p"
    if re.search(r"\bHD\b", text, flags=re.I):
        return "720p"
    return "Other"


def find_codec(text: str) -> str:
    choices = (
        ("HEVC/x265", r"\b(?:x265|h\.?265|hevc)\b"),
        ("AVC/x264", r"\b(?:x264|h\.?264|avc)\b"),
        ("AV1", r"\bav1\b"),
        ("XviD", r"\bxvid\b"),
    )
    for label, pattern in choices:
        if re.search(pattern, text, flags=re.I):
            return label
    return ""


def find_bit_depth(text: str) -> str:
    match = re.search(r"\b(8|10|12)\s*(?:bit|bits)\b", text, flags=re.I)
    return f"{match.group(1)}-bit" if match else ""


def find_hdr(text: str) -> str:
    labels = (
        ("Dolby Vision", r"\b(?:dolby\s*vision|dovi|dv)\b"),
        ("HDR10+", r"\bhdr10\+\b"),
        ("HDR10", r"\bhdr10\b"),
        ("HDR", r"\bhdr\b"),
    )
    for label, pattern in labels:
        if re.search(pattern, text, flags=re.I):
            return label
    return ""


def find_language(text: str) -> str:
    # The storage channel is dedicated to Sinhala subtitles. Explicit tags are
    # still recognised, but untagged team uploads remain Sinhala by design.
    return "Sinhala"


def find_release_group(raw: str) -> str:
    stem = PurePath(raw or "").stem
    # Remove trailing duplicate counters such as "(1)" first.
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem).strip()
    # Common scene/release notation: ... - YTS.MX / - PSA / - RARBG.
    match = re.search(r"(?:\s[-–—]\s|[-–—])\s*\[?([A-Za-z0-9][A-Za-z0-9_.-]{1,30})\]?$", stem)
    if not match:
        return ""
    candidate = match.group(1).replace("_", "").strip(".- ")
    ignored = {
        "srt", "ass", "ssa", "vtt", "zip", "x264", "x265", "hevc", "avc", "av1",
        "10bit", "8bit", "12bit", "si", "sinhala", "sinhalese", "web", "webrip", "webdl",
        "bluray", "bdrip", "hdrip", "hdtv", "cam", "hdts", "other",
    }
    return "" if candidate.lower().replace("-", "") in ignored else candidate


def parse_caption_value(caption: str, label: str) -> str:
    pattern = rf"(?:^|\n)\s*{re.escape(label)}\s*:\s*(.+?)\s*(?=\n|$)"
    match = re.search(pattern, caption or "", flags=re.I)
    return match.group(1).strip() if match else ""


def _caption_any_value(caption: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        value = parse_caption_value(caption, label)
        if value:
            return value
    return ""


def _explicit_caption_release(caption: str) -> str:
    """Build a release string from human caption fields or a pasted video filename.

    Subtitle makers often upload a small .srt file whose name does not contain
    enough information.  In that case the Telegram caption may contain the video
    release name, or fields such as Name/Year/S01/E04/Quality.  This converts
    those formats into the same release text parsed from normal filenames.
    """
    raw_caption = (caption or "").replace("\u00a0", " ").strip()
    if not raw_caption:
        return ""

    name = _caption_any_value(raw_caption, ("Name", "Title", "Movie", "Movie name", "Series", "TV Show", "Show"))
    year = _caption_any_value(raw_caption, ("Year", "Release year"))
    quality = _caption_any_value(raw_caption, ("Quality", "Resolution"))
    season = _caption_any_value(raw_caption, ("Season", "Season Number", "Season no"))
    episode = _caption_any_value(raw_caption, ("Episode", "Episode Number", "Episode no", "Ep"))
    source = _caption_any_value(raw_caption, ("Source", "Rip", "Release"))
    codec = _caption_any_value(raw_caption, ("Codec", "Video", "Format"))
    audio = _caption_any_value(raw_caption, ("Audio", "Audio format"))

    if name:
        parts = [name]
        if year:
            year_match = re.search(r"\b(?:19|20)\d{2}\b", year)
            if year_match:
                parts.append(year_match.group(0))
        season_digits = re.search(r"\d{1,3}", season or "")
        episode_digits = re.search(r"\d{1,4}", episode or "")
        if season_digits and episode_digits:
            parts.append(f"S{int(season_digits.group(0)):02d}E{int(episode_digits.group(0)):02d}")
        elif season_digits:
            parts.append(f"S{int(season_digits.group(0)):02d}")
        if quality:
            parts.append(quality)
        if source:
            parts.append(source)
        if codec:
            parts.append(codec)
        if audio:
            parts.append(audio)
        return " ".join(str(part).strip() for part in parts if str(part).strip())

    # Otherwise use the first caption line that looks like a pasted release /
    # video filename.  Backticks and bullets are ignored so Telegram formatting
    # does not break parsing.
    for line in raw_caption.splitlines():
        candidate = line.strip().strip("`*_•-–— ")
        if not candidate:
            continue
        if re.search(r"\.(?:mkv|mp4|avi|mov|webm|srt|ass|ssa|vtt|zip)\b", candidate, flags=re.I):
            return candidate

        # Quality/resolution is NOT required. Accept the two minimum useful
        # release forms too:
        #   Movie.Name.2023
        #   Show.Name.S01E04
        # Optional tags such as 720p, WEB-DL, x265, DDP5.1 still improve the
        # release details, but they are no longer needed for auto matching.
        has_letters = bool(re.search(r"[A-Za-z]", candidate))
        has_year = bool(re.search(r"\b(?:19|20)\d{2}\b", candidate))
        has_episode = bool(re.search(r"\bS\d{1,3}\s*E\d{1,4}\b|\b\d{1,2}\s*[xX]\s*\d{1,4}\b", candidate, flags=re.I))
        if has_letters and (has_year or has_episode):
            return candidate

        marker_hits = sum(1 for pattern in _RELEASE_MARKERS if re.search(pattern, candidate, flags=re.I))
        if marker_hits >= 2 or has_year and marker_hits >= 1:
            return candidate
    return ""


def _looks_generic_title(title: str) -> bool:
    clean = re.sub(r"[^A-Za-z0-9]+", " ", title or "").strip().lower()
    if not clean:
        return True
    generic = {
        "subtitle", "subtitles", "sinhala", "sinhalese", "si", "srt", "ass",
        "ssa", "vtt", "zip", "untitled", "movie subtitle", "series subtitle",
    }
    if clean in generic:
        return True
    words = clean.split()
    return len(words) <= 2 and any(word in generic for word in words)


def _merge_title_year(title: str, release_text: str) -> str:
    if re.search(r"\b(?:19|20)\d{2}\b", title or ""):
        return title
    year = re.search(r"\b(?:19|20)\d{2}\b", release_text or "")
    if year and not re.search(r"\bS\d{1,3}\s*E\d{1,4}\b", release_text or "", flags=re.I):
        return f"{title} {year.group(0)}".strip()
    return title


def _title_from_filename(normal: str) -> str:
    end = _first_match_position(normal, _RELEASE_MARKERS)
    candidate = normal[:end] if end is not None else normal
    # Creator releases often split the title with dashes. Keep a final year but
    # remove empty separators and explicit subtitle labels.
    candidate = re.sub(r"(?:^|\s-\s)(?:sub(?:title)?s?|esub(?:s)?)\b.*$", "", candidate, flags=re.I)
    candidate = re.sub(r"\s*[-–—]\s*$", "", candidate).strip(" -_|[]{}")
    candidate = re.sub(r"\s+", " ", candidate)
    return candidate or "Untitled"


def parse_subtitle_name(filename: str, caption: str = "") -> SubtitleGuess:
    filename_normal = _normalise(filename)
    caption_release = _explicit_caption_release(caption)
    caption_normal = _normalise(caption_release) if caption_release else ""

    file_season, file_episode, file_episode_end = _extract_episode(filename_normal)
    cap_season, cap_episode, cap_episode_end = _extract_episode(caption_normal) if caption_normal else (None, None, None)

    file_title = _title_from_filename(filename_normal)
    cap_title = _title_from_filename(caption_normal) if caption_normal else ""
    explicit_title = _caption_any_value(caption or "", ("Name", "Title", "Movie", "Movie name", "Series", "TV Show", "Show"))

    if explicit_title:
        title_guess = explicit_title
    elif cap_title and (_looks_generic_title(file_title) or bool(cap_episode and not file_episode) or find_resolution(filename_normal) == "Other"):
        title_guess = cap_title
    else:
        title_guess = file_title

    release_for_year = caption_normal or filename_normal
    title_guess = _merge_title_year(re.sub(r"\s+", " ", title_guess).strip(), release_for_year)

    season = file_season if file_season is not None else cap_season
    episode = file_episode if file_episode is not None else cap_episode
    episode_end = file_episode_end if file_episode_end is not None else cap_episode_end

    note = parse_caption_value(caption, "Note") or parse_caption_value(caption, "Review")
    full_text = f"{filename}\n{caption_release}\n{caption}"
    return SubtitleGuess(
        filename=filename,
        title_guess=title_guess or "Untitled",
        source_type=find_source(full_text),
        resolution=find_resolution(full_text),
        season=season,
        episode=episode,
        episode_end=episode_end,
        language=find_language(full_text),
        note=note,
        codec=find_codec(full_text),
        bit_depth=find_bit_depth(full_text),
        hdr=find_hdr(full_text),
        release_group=find_release_group(caption_release or filename),
    )


def caption_uploader(caption: str) -> str:
    # Supports both metadata captions and a compact channel header with @username.
    raw = parse_caption_value(caption, "Uploader") or parse_caption_value(caption, "Maker") or (caption or "")
    match = re.search(r"@([A-Za-z0-9_]{5,32})", raw)
    return clean_username(match.group(1) if match else raw)


def caption_tmdb(caption: str) -> tuple[str, int] | None:
    raw = parse_caption_value(caption, "TMDB")
    match = re.search(r"\b(movie|tv)\s*:\s*(\d+)\b", raw, re.I)
    return (match.group(1).lower(), int(match.group(2))) if match else None


def caption_source(caption: str) -> str:
    raw = parse_caption_value(caption, "Source")
    return raw if raw in SOURCE_TYPES else ""


def caption_resolution(caption: str) -> str:
    raw = parse_caption_value(caption, "Resolution")
    return raw if raw in RESOLUTIONS else ""
