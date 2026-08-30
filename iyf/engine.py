"""iyf.lv scraping and yt-dlp download helpers."""

import html
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import httpx
import yt_dlp
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

HOST = "https://www.iyf.lv"
DEFAULT_LINE = 1
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36"
)
REFERER = f"{HOST}/"

RE_IYFPLAY = re.compile(r"/iyfplay/(\d+)-(\d+)-(\d+)/?")
RE_IYFTV = re.compile(r"/iyftv/(\d+)/?")
RE_NUMERIC = re.compile(r"^\d+$")
RE_SEASON = re.compile(r"第([一二三四五六七八九十百]+)季")
RE_SEASON_TOKEN = re.compile(r"第[一二三四五六七八九十百]+季")


@dataclass
class SearchResult:
    """A show returned by the iyf.lv search."""

    show_id: str
    title: str


@dataclass
class Episode:
    """An episode number and its title."""

    number: str
    title: str


@dataclass
class Line:
    """One iyf streaming line and its episodes."""

    number: int
    episodes: list[Episode]


@dataclass
class Series:
    """A show/movie detail page and its available lines."""

    show_id: str
    title: str
    lines: list[Line]

    def line(self, number: int) -> Line | None:
        return next((item for item in self.lines if item.number == number), None)


@dataclass
class Source:
    """A normalized show, optionally carrying line and episode information."""

    show_id: str
    line: int | None = None
    episode: int | None = None


@dataclass
class PlayInfo:
    """A playable stream and its iyf media class."""

    stream_url: str
    media_class: str | None = None


class DownloadParams(TypedDict, total=False):
    format: str
    outtmpl: str
    noplaylist: bool
    no_warnings: bool
    quiet: bool
    hls_prefer_native: bool
    concurrent_fragment_downloads: int
    progress_hooks: list[Callable[[dict[str, object]], None]]


class IyfError(RuntimeError):
    """An expected iyf or download failure."""


def _http_get(url: str, timeout: float = 30.0) -> str:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": REFERER},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text
    except httpx.HTTPStatusError as error:
        raise IyfError(f"HTTP {error.response.status_code} fetching {url}") from error
    except httpx.HTTPError as error:
        raise IyfError(f"Network error fetching {url}: {error}") from error


def _strip_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _episode_title(text: str, match: re.Match[str], episode: str) -> str:
    anchor_start = text.rfind("<a", 0, match.start())
    anchor_end = text.find("</a>", match.end())
    if anchor_start >= 0 and anchor_end >= 0:
        content_start = text.find(">", anchor_start, match.start())
        if content_start >= 0:
            title = _strip_tags(text[content_start + 1 : anchor_end])
            if title:
                return title
    return f"第{episode}集"


def search(query: str) -> list[SearchResult]:
    """Return first-page iyf.lv search results."""
    url = f"{HOST}/s/{httpx.URL(query)}-------------.html"
    text = _http_get(url)
    results: list[SearchResult] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'class="module-card-item-title"[^>]*>\s*'
        r'<a href="/iyftv/(\d+)/"[^>]*>(.*?)</a>',
        re.S,
    )
    for match in pattern.finditer(text):
        show_id, title = match.group(1), _strip_tags(match.group(2))
        if show_id not in seen and title:
            seen.add(show_id)
            results.append(SearchResult(show_id, title))
    return results


def get_show(show_id: str) -> Series:
    """Return show metadata and episode titles grouped by line."""
    text = _http_get(f"{HOST}/iyftv/{show_id}/")
    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)
    title = _strip_tags(title_match.group(1)) if title_match else show_id

    lines: list[Line] = []
    seen: set[str] = set()
    for match in RE_IYFPLAY.finditer(text):
        if match.group(1) != show_id:
            continue
        line_number, episode_number = int(match.group(2)), match.group(3)
        key = f"{line_number}:{episode_number}"
        if key in seen:
            continue
        seen.add(key)
        line = next((item for item in lines if item.number == line_number), None)
        if line is None:
            line = Line(line_number, [])
            lines.append(line)
        line.episodes.append(
            Episode(episode_number, _episode_title(text, match, episode_number)),
        )
        line.episodes.sort(key=lambda item: int(item.number))
    lines.sort(key=lambda item: item.number)
    return Series(show_id, title, lines)


def get_play_info(show_id: str, line: int, episode: str) -> PlayInfo:
    """Resolve one episode to its HLS URL and iyf ``vod_class``."""
    url = f"{HOST}/iyfplay/{show_id}-{line}-{episode}/"
    data = _extract_player_json(_http_get(url))
    stream = data.get("url") if data else None
    if not isinstance(stream, str) or not stream.startswith("http"):
        raise IyfError(
            f"no playable stream for episode {episode} on line {line} (show {show_id})"
        )
    vod_data = _json_object(data.get("vod_data")) if data else None
    vod_class = vod_data.get("vod_class") if vod_data else None
    return PlayInfo(
        stream_url=stream,
        media_class=vod_class if isinstance(vod_class, str) else None,
    )


def _extract_player_json(text: str) -> dict[str, object] | None:
    marker = "var player_aaaa"
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    while start < len(text) and text[start] in " \t\r\n=":
        start += 1
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    in_string = False
    escaped = False
    for end in range(start, len(text)):
        char = text[end]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = _json_object(json.loads(text[start : end + 1]))
                except json.JSONDecodeError:
                    return None
                return value
    return None


def get_play_url(show_id: str, line: int, episode: str) -> str:
    """Resolve one episode to its embedded HLS URL."""
    return get_play_info(show_id, line, episode).stream_url


def parse_input(value: str) -> Source:
    """Return a normalized source from an iyf.lv URL or numeric id."""
    value = value.strip()
    match = RE_IYFPLAY.search(value)
    if match:
        return Source(match.group(1), int(match.group(2)), int(match.group(3)))
    match = RE_IYFTV.search(value)
    if match:
        return Source(match.group(1))
    if RE_NUMERIC.fullmatch(value):
        return Source(value)
    raise IyfError(f"unsupported iyf.lv link: {value!r}")


def pick_episode(episodes: list[Episode], url_episode: int | None) -> Episode:
    """Use the URL episode, or the first listed episode."""
    if url_episode is not None:
        for episode in episodes:
            if int(episode.number) == url_episode:
                return episode
        raise IyfError(f"episode {url_episode} not found on this line")
    if not episodes:
        raise IyfError("show has no episodes")
    return episodes[0]


def parse_chinese_numeral(value: str) -> int | None:
    digits = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if len(value) == 1:
        return digits.get(value)
    if value.startswith("十") and len(value) == 2:
        return 10 + (digits.get(value[1]) or 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        tens_value = digits.get(tens)
        if tens_value is None:
            return None
        return tens_value * 10 + (digits.get(ones) or 0)
    return None


def season_from_title(title: str) -> int:
    match = RE_SEASON.search(title)
    if not match:
        return 1
    return parse_chinese_numeral(match.group(1)) or 1


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return name.strip().strip(".") or "video"


def download(
    media_url: str,
    out_path: str,
    fmt: str = "best",
    verbose: bool = False,
    concurrent_fragments: int = 8,
    progress_cb: Callable[[float], None] | None = None,
    show_progress: bool = True,
) -> None:
    """Download an HLS URL with yt-dlp and display Rich progress."""
    if concurrent_fragments < 1:
        raise IyfError("concurrent_fragments must be at least 1")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>5.1f}%"),
        TimeRemainingColumn(),
        disable=not show_progress,
    )
    options: DownloadParams = {
        "format": fmt,
        "outtmpl": out_path,
        "noplaylist": True,
        "no_warnings": not verbose,
        "quiet": not verbose,
        "hls_prefer_native": True,
        "concurrent_fragment_downloads": concurrent_fragments,
    }
    with progress:
        task_id = progress.add_task(Path(out_path).name, total=100.0)

        def progress_hook(data: dict[str, object]) -> None:
            status = data.get("status")
            if status not in {"downloading", "finished"}:
                return
            if status == "finished":
                percentage = 100.0
            else:
                value = data.get("_percent_str")
                if isinstance(value, str):
                    try:
                        percentage = float(value.replace("%", "").strip())
                    except ValueError:
                        return
                else:
                    raw_percentage = data.get("_percent")
                    if isinstance(raw_percentage, (int, float)):
                        percentage = float(raw_percentage)
                    else:
                        fragment_index = data.get("fragment_index")
                        fragment_count = data.get("fragment_count")
                        if not (
                            isinstance(fragment_index, int)
                            and isinstance(fragment_count, int)
                            and fragment_count > 0
                        ):
                            return
                        percentage = fragment_index / fragment_count * 100
            progress.update(task_id, completed=percentage)
            if progress_cb is not None:
                progress_cb(percentage)

        options["progress_hooks"] = [progress_hook]
        try:
            with yt_dlp.YoutubeDL(options) as ydl:  # pyright: ignore[reportArgumentType]
                result = ydl.download([media_url])
        except Exception as error:
            raise IyfError(f"yt-dlp failed: {error}") from error
        if result != 0:
            raise IyfError(f"yt-dlp exited with code {result}")
