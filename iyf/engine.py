"""iyf.lv scraping and yt-dlp download helpers."""

import html
import json
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from glob import escape as glob_escape
from glob import glob
from pathlib import Path
from typing import TypedDict
from urllib.parse import urljoin

import httpx
import yt_dlp
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

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


@dataclass(frozen=True)
class PlaylistQuality:
    """Declared quality metadata from an HLS playlist."""

    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
    frame_rate: float | None = None


@dataclass(frozen=True)
class PlaylistInspection:
    """Validity and declared quality found in one HLS playlist."""

    valid: bool
    quality: PlaylistQuality | None = None
    duration: float = 0.0

    @property
    def declared(self) -> bool:
        return self.quality is not None


_MAX_PLAYLIST_EXPANSIONS = 4
_MAX_PLAYLIST_REQUESTS_PER_LINE = 5


def _hls_attribute(tag: str, name: str) -> str | None:
    match = re.search(rf"(?:^|[:,]){name}=(\"[^\"]*\"|[^,]+)", tag)
    if not match:
        return None
    return match.group(1).strip('"')


def playlist_quality_key(quality: PlaylistQuality | None) -> tuple[float, ...]:
    """Return the declared-quality ordering used for line selection."""
    if quality is None:
        return (-1, -1, -1, -1)
    return (
        quality.height if quality.height is not None else -1,
        quality.width if quality.width is not None else -1,
        quality.bandwidth if quality.bandwidth is not None else -1,
        quality.frame_rate if quality.frame_rate is not None else -1,
    )


def playlist_quality_label(inspection: PlaylistInspection) -> str:
    """Return a concise human-readable explanation for a selected playlist."""
    quality = inspection.quality
    if quality is None:
        return "未声明画质"
    if quality.width is not None and quality.height is not None:
        return f"{quality.width}x{quality.height}"
    if quality.bandwidth is not None:
        return f"{quality.bandwidth} bps"
    return "已声明质量"


def _playlist_duration(lines: list[str]) -> float:
    durations: list[float] = []
    for line in lines:
        if not line.startswith("#EXTINF:"):
            continue
        raw_duration = line.removeprefix("#EXTINF:").split(",", 1)[0]
        try:
            durations.append(float(raw_duration))
        except ValueError:
            continue
    return sum(durations)


@dataclass
class _PlaylistRequestContext:
    requests: int = 0
    visited: set[str] = field(default_factory=set)
    responses: dict[str, str | None] = field(default_factory=dict)
    inspections: dict[str, PlaylistInspection] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)

    def fetch(self, url: str) -> str | None:
        if url in self.responses:
            return self.responses[url]
        if self.requests >= _MAX_PLAYLIST_REQUESTS_PER_LINE:
            return None
        self.visited.add(url)
        self.requests += 1
        try:
            text = _http_get(url)
        except IyfError:
            text = None
        self.responses[url] = text
        return text


def _inspect_playlist(
    text: str,
    base_url: str | None,
    depth: int,
    context: _PlaylistRequestContext,
) -> PlaylistInspection:
    if base_url:
        if base_url in context.inspections:
            return context.inspections[base_url]
        if base_url in context.active:
            return PlaylistInspection(False)
        context.active.add(base_url)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[tuple[PlaylistQuality, str]] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        uri = next(
            (item for item in lines[index + 1 :] if not item.startswith("#")),
            None,
        )
        if uri is None:
            continue
        resolution = _hls_attribute(line, "RESOLUTION")
        width: int | None = None
        height: int | None = None
        if resolution and "x" in resolution:
            raw_width, raw_height = resolution.split("x", 1)
            if raw_width.isdigit() and raw_height.isdigit():
                width, height = int(raw_width), int(raw_height)
        bandwidth = _hls_attribute(line, "BANDWIDTH")
        frame_rate = _hls_attribute(line, "FRAME-RATE")
        try:
            parsed_frame_rate = float(frame_rate) if frame_rate else None
        except ValueError:
            parsed_frame_rate = None
        variants.append(
            (
                PlaylistQuality(
                    width=width,
                    height=height,
                    bandwidth=int(bandwidth)
                    if bandwidth and bandwidth.isdigit()
                    else None,
                    frame_rate=parsed_frame_rate,
                ),
                uri,
            )
        )

    if variants:
        ordered_variants = sorted(
            variants,
            key=lambda item: playlist_quality_key(item[0]),
            reverse=True,
        )
        first_quality = ordered_variants[0][0]
        result = PlaylistInspection(False, first_quality)
        if depth < _MAX_PLAYLIST_EXPANSIONS:
            for quality, uri in ordered_variants:
                variant_url = urljoin(base_url or "", uri)
                if not variant_url.startswith(("http://", "https://")):
                    continue
                child_text = context.fetch(variant_url)
                if child_text is None:
                    continue
                child = _inspect_playlist(child_text, variant_url, depth + 1, context)
                if child.valid:
                    result = PlaylistInspection(
                        True,
                        quality if quality != PlaylistQuality() else None,
                        child.duration,
                    )
                    break
    else:
        result = PlaylistInspection(
            _playlist_duration(lines) > 0,
            duration=_playlist_duration(lines),
        )

    if base_url:
        context.active.discard(base_url)
        context.inspections[base_url] = result
    return result


def inspect_playlist(
    text: str, base_url: str | None = None, _depth: int = 0
) -> PlaylistInspection:
    """Validate an HLS playlist and return its declared quality metadata."""
    context = _PlaylistRequestContext(requests=1)
    if base_url:
        context.visited.add(base_url)
        context.responses[base_url] = text
    return _inspect_playlist(text, base_url, _depth, context)


def inspect_playlist_url(url: str) -> PlaylistInspection:
    """Fetch and inspect one line's playlist within its request budget."""
    context = _PlaylistRequestContext()
    text = context.fetch(url)
    if text is None:
        return PlaylistInspection(False)
    return _inspect_playlist(text, url, 0, context)


class DownloadParams(TypedDict, total=False):
    format: str
    outtmpl: str
    noplaylist: bool
    no_warnings: bool
    quiet: bool
    noprogress: bool
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


@dataclass
class ProgressSample:
    """One progress observation for a single download."""

    downloaded: int | None = None
    total: int | None = None
    speed: float | None = None
    finished: bool = False


_SIZE_PROBE_WORKERS = 32
_MAX_PROBE_PLAYLIST_BYTES = 2_000_000


def _playlist_segments(text: str, base_url: str) -> tuple[list[str], list[float]]:
    """Return segment URLs and their durations from a media playlist."""
    urls: list[str] = []
    durations: list[float] = []
    pending = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line[len("#EXTINF:") :].split(",")[0])
            except ValueError:
                pending = 0.0
            if not math.isfinite(pending) or pending < 0:
                # A malformed or infinite duration must not reach the
                # arithmetic below: that raises instead of returning None
                # like every other unusable input.
                pending = 0.0
        elif not line.startswith("#"):
            urls.append(urllib.parse.urljoin(base_url, line))
            durations.append(pending)
            pending = 0.0
    return urls, durations


def _best_variant(media_url: str, text: str) -> str | None:
    """Return the highest-bandwidth variant URL declared by a master playlist."""
    best_url: str | None = None
    best_bandwidth = -1
    bandwidth = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"BANDWIDTH=(\d+)", line)
            bandwidth = int(match.group(1)) if match else 0
        elif line and not line.startswith("#"):
            if bandwidth > best_bandwidth:
                best_bandwidth = bandwidth
                best_url = urllib.parse.urljoin(media_url, line)
            bandwidth = 0
    return best_url


def probe_total_bytes(media_url: str, deadline: float = 6.0) -> int | None:
    """Measure the payload size of an HLS VOD stream in bytes.

    yt-dlp has no size for HLS up front, and its ``total_bytes_estimate`` is
    extrapolated from finished fragments: on a measured 310.6 MB episode it
    started at 789 MB and only converged at the very end. Sampling segment
    sizes instead is not good enough either, because segment sizes here run
    from 100 KB to 11 MB over 0.8-20 s of media: an 8-to-64 segment sample
    landed 2.5-17% off, and even a 294-of-881 sample was 2% off. Asking every
    segment for its ``Content-Length`` cost 2 s for 128 segments and 3.3 s for
    881 with 32 workers, and matched the downloaded payload to within a
    kilobyte.

    ``deadline`` bounds every network phase: playlist reads use the remaining
    time, an expired deadline skips them, and HEAD requests are submitted in
    windows that stop at the deadline. Local parsing is the one phase it
    cannot interrupt, so a playlist larger than ``_MAX_PROBE_PLAYLIST_BYTES``
    is refused outright. Whatever answered in time is scaled by the media time
    it covers, which makes the result an estimate rather than a measurement;
    ``None`` means no usable answer at all, and callers then show bytes
    without a denominator.

    In-flight HEAD requests are abandoned rather than awaited, so they can
    outlive the call by their own per-request timeout.
    """
    started = time.monotonic()

    def remaining() -> float:
        return max(deadline - (time.monotonic() - started), 0.0)

    def fetch(url: str) -> str | None:
        """Read one playlist, bounded by the deadline and by its own size.

        Parsing is local work the deadline cannot interrupt, so an oversized
        playlist is refused here, before any master or media playlist parsing.
        """
        left = remaining()
        if left <= 0:
            return None
        try:
            text = _http_get(url, timeout=left)
        except IyfError:
            return None
        if len(text) > _MAX_PROBE_PLAYLIST_BYTES:
            return None
        return text

    text = fetch(media_url)
    if text is None:
        return None
    if "#EXT-X-STREAM-INF" in text:
        variant = _best_variant(media_url, text)
        if variant is None:
            return None
        text = fetch(variant)
        if text is None:
            return None
        media_url = variant
    if "#EXT-X-ENDLIST" not in text:
        # A live or event playlist keeps growing, so its current segment list
        # is not this download's total.
        return None
    urls, durations = _playlist_segments(text, media_url)
    if not urls or any(duration <= 0 for duration in durations):
        # A segment with a missing, zero or malformed duration cannot be
        # scaled, and skipping it would silently drop its bytes from the
        # total: no denominator is better than a wrong one.
        return None

    total_duration = sum(durations)

    def measure(index: int) -> tuple[int, float]:
        try:
            response = httpx.head(
                urls[index],
                headers={"User-Agent": USER_AGENT, "Referer": REFERER},
                timeout=3.0,
                follow_redirects=True,
            )
            # A 404 or 403 can still carry a Content-Length; only a real
            # segment answer may contribute bytes.
            response.raise_for_status()
            return max(int(response.headers.get("content-length", 0)), 0), durations[
                index
            ]
        except httpx.HTTPError, ValueError:
            return 0, durations[index]

    pool = ThreadPoolExecutor(max_workers=_SIZE_PROBE_WORKERS)
    measured: list[tuple[int, float]] = []
    try:
        # Submit in windows so a huge playlist cannot queue thousands of
        # requests past the deadline, and check the clock between windows.
        window = _SIZE_PROBE_WORKERS * 4
        for start in range(0, len(urls), window):
            if remaining() <= 0:
                break
            batch = [
                pool.submit(measure, index)
                for index in range(start, min(start + window, len(urls)))
            ]
            done, pending = wait(batch, timeout=remaining())
            for future in pending:
                future.cancel()
            measured.extend(
                future.result() for future in done if future.exception() is None
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    usable = [
        (size, duration) for size, duration in measured if size > 0 and duration > 0
    ]
    if len(usable) < min(2, len(urls)):
        return None
    measured_bytes = sum(size for size, _ in usable)
    if len(usable) == len(urls):
        return measured_bytes
    covered = sum(duration for _, duration in usable)
    return int(measured_bytes / covered * total_duration)


def _part_bytes(out_path: str) -> int:
    """Return the bytes written for ``out_path``, in-flight fragments included."""
    total = 0
    for path in (f"{out_path}.part", *glob(f"{glob_escape(out_path)}.part-*")):
        try:
            total += Path(path).stat().st_size
        except OSError:
            continue
    return total


def _watch_part_file(
    out_path: str,
    total: int | None,
    emit: Callable[[ProgressSample], None],
    hooked: threading.Event,
    stop: threading.Event,
    grace: float = 3.0,
    interval: float = 0.5,
) -> None:
    """Report progress from the growing ``.part`` file while yt-dlp stays silent.

    yt-dlp's external downloaders call the progress hook once, at the very
    end: ffmpeg downloads AES-128 HLS that way when pycryptodomex is missing.
    For those runs the output file is the only live signal.
    """
    if hooked.wait(grace):
        return
    last_size = 0
    last_time = time.monotonic()
    while not stop.wait(interval):
        if hooked.is_set():
            return
        size = _part_bytes(out_path)
        if size <= 0:
            continue
        now = time.monotonic()
        elapsed = now - last_time
        speed = (
            (size - last_size) / elapsed if elapsed > 0 and size > last_size else None
        )
        last_size, last_time = size, now
        emit(ProgressSample(downloaded=size, total=total, speed=speed))


def download(
    media_url: str,
    out_path: str,
    fmt: str = "best",
    verbose: bool = False,
    concurrent_fragments: int = 8,
    progress_cb: Callable[[ProgressSample], None] | None = None,
    show_progress: bool = True,
    total_bytes: int | None = None,
) -> None:
    """Download an HLS URL with yt-dlp and report bytes, speed and progress."""
    if concurrent_fragments < 1:
        raise IyfError("concurrent_fragments must be at least 1")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    reporting = progress_cb is not None or show_progress
    total = total_bytes
    if total is None and reporting:
        # Probing before the download starts keeps these HEAD requests out of
        # the fragment traffic: run concurrently they compete for the same
        # connections and can outlast the download, leaving the display
        # without a denominator.
        total = probe_total_bytes(media_url)
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        disable=not show_progress,
    )
    options: DownloadParams = {
        "format": fmt,
        "outtmpl": out_path,
        "noplaylist": True,
        "no_warnings": not verbose,
        "quiet": not verbose,
        # yt-dlp's own progress bar ignores ``quiet``; only ``noprogress``
        # silences it, and the native HLS downloader prints one.
        "noprogress": not verbose,
        "hls_prefer_native": True,
        "concurrent_fragment_downloads": concurrent_fragments,
    }
    hooked = threading.Event()
    stop = threading.Event()
    with progress:
        task_id = progress.add_task(
            Path(out_path).name, total=float(total) if total else None
        )

        def emit(sample: ProgressSample) -> None:
            if show_progress:
                if sample.total is not None:
                    progress.update(task_id, total=float(sample.total))
                if sample.downloaded is not None:
                    completed = float(sample.downloaded)
                    if sample.total:
                        completed = min(completed, float(sample.total))
                    progress.update(task_id, completed=completed)
            if progress_cb is not None:
                progress_cb(sample)

        def progress_hook(data: dict[str, object]) -> None:
            status = data.get("status")
            if status not in {"downloading", "finished"}:
                return
            hooked.set()
            downloaded = data.get("downloaded_bytes")
            speed = data.get("speed")
            emit(
                ProgressSample(
                    downloaded=int(downloaded)
                    if isinstance(downloaded, (int, float))
                    else None,
                    total=total,
                    speed=float(speed) if isinstance(speed, (int, float)) else None,
                    finished=status == "finished",
                )
            )

        options["progress_hooks"] = [progress_hook]
        watcher: threading.Thread | None = None
        if reporting:
            watcher = threading.Thread(
                target=_watch_part_file,
                args=(out_path, total, emit, hooked, stop),
                name="iyf-size-watch",
                daemon=True,
            )
            watcher.start()
        try:
            with yt_dlp.YoutubeDL(options) as ydl:  # pyright: ignore[reportArgumentType]
                result = ydl.download([media_url])
        except Exception as error:
            raise IyfError(f"yt-dlp failed: {error}") from error
        finally:
            stop.set()
        if watcher is not None:
            watcher.join(timeout=2.0)
        if result != 0:
            raise IyfError(f"yt-dlp exited with code {result}")
