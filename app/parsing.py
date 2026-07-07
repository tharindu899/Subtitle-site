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
    normal = _normalise(filename)
    season, episode, episode_end = _extract_episode(normal)
    title_guess = _title_from_filename(normal)
    note = parse_caption_value(caption, "Note") or parse_caption_value(caption, "Review")
    full_text = f"{filename}\n{caption}"
    return SubtitleGuess(
        filename=filename,
        title_guess=title_guess,
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
        release_group=find_release_group(filename),
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
