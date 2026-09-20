"""Python API for downloading videos from iyf.tv and iyf.lv."""

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import httpx

from . import engine, normalize
from .progress import ProgressRenderer

__version__ = "0.3.0"
_ALLOWED_HOSTS = {"iyf.tv", "www.iyf.tv", "iyf.lv", "www.iyf.lv"}


@dataclass
class Video:
    """Resolved video metadata."""

    link: str
    show_id: str
    line: int
    show_name: str
    season: int
    episode: str
    episode_title: str
    media_class: str | None
    stream_url: str
    is_series: bool = True

    @property
    def filename(self) -> str:
        """Return the filesystem-safe mp4 filename for this video."""
        return self._library_filename("mp4")

    @property
    def stream_filename(self) -> str:
        """Return the filesystem-safe ``.strm`` filename for this video."""
        return self._library_filename("strm")

    def _library_filename(self, extension: str) -> str:
        if not self.is_series:
            # A single-video entry (a film or documentary) must not look like
            # an episode, or a media server files it as season 1 episode 1.
            return engine.sanitize_filename(f"{self.show_name}.{extension}")
        return engine.sanitize_filename(
            f"{self.show_name} S{self.season:02d}E{int(self.episode):02d}.{extension}"
        )


def _supported_url(link: str) -> bool:
    try:
        parsed = httpx.URL(link)
    except httpx.InvalidURL:
        return False
    return bool(parsed.scheme and parsed.host)


def _validate_link(link: str) -> str:
    try:
        parsed = httpx.URL(link)
    except httpx.InvalidURL as error:
        raise engine.IyfError("link must be an http(s) iyf.tv or iyf.lv URL") from error
    if parsed.scheme not in {"http", "https"} or parsed.host not in _ALLOWED_HOSTS:
        raise engine.IyfError("link must be an http(s) iyf.tv or iyf.lv URL")
    return link


def query(text: str) -> list[engine.SearchResult]:
    """Search iyf.lv and return matching shows."""
    text = text.strip()
    if not text:
        raise engine.IyfError("query must not be empty")
    return engine.search(text)


_MAX_QUALITY_SEARCH_MATCHES = 3
_MAX_QUALITY_LINES_PER_MATCH = 6


@dataclass(frozen=True)
class _SourceSelection:
    search_index: int
    result: engine.SearchResult
    series: engine.Series
    line: engine.Line
    inspection: engine.PlaylistInspection

    @property
    def source(self) -> engine.Source:
        return engine.Source(self.result.show_id, line=self.line.number)


def _inspect_line(show_id: str, line: engine.Line) -> engine.PlaylistInspection | None:
    if not line.episodes:
        return None
    try:
        stream_url = engine.get_play_info(
            show_id, line.number, line.episodes[0].number
        ).stream_url
        inspection = engine.inspect_playlist_url(stream_url)
    except engine.IyfError:
        return None
    return inspection if inspection.valid else None


def _select_quality_line(
    series: engine.Series,
) -> tuple[engine.Line, engine.PlaylistInspection] | None:
    fallback: tuple[engine.Line, engine.PlaylistInspection] | None = None
    best: tuple[engine.Line, engine.PlaylistInspection] | None = None
    # Six lines covers current shows while bounding each match's fan-out.
    for line in sorted(series.lines, key=lambda item: item.number)[
        :_MAX_QUALITY_LINES_PER_MATCH
    ]:
        # Tags cannot reveal cadence when equal (as on lines 2/3), so sorted
        # line order makes the lower line number the deterministic tie-break.
        inspection = _inspect_line(series.show_id, line)
        if inspection is None:
            continue
        if fallback is None:
            fallback = (line, inspection)
        if not inspection.declared:
            continue
        if best is None or engine.playlist_quality_key(
            inspection.quality
        ) > engine.playlist_quality_key(best[1].quality):
            best = (line, inspection)
    return best or fallback


def _pick_query_source(
    source: str, matches: list[engine.SearchResult] | None = None
) -> _SourceSelection | None:
    matches = query(source) if matches is None else matches
    if not matches:
        raise engine.IyfError(f"no matches for query {source!r}")

    candidates: list[_SourceSelection] = []
    # Quality selection covers at most 3 matches × 6 lines = 18 line checks.
    # Each line uses 1 root playlist + up to 4 expansion requests: 90 playlist
    # requests total. Variants beyond the remaining budget are not attempted.
    for search_index, match in enumerate(matches[:_MAX_QUALITY_SEARCH_MATCHES]):
        try:
            series = engine.get_show(match.show_id)
        except engine.IyfError:
            continue
        choice = _select_quality_line(series)
        if choice is None:
            continue
        line, inspection = choice
        candidates.append(
            _SourceSelection(search_index, match, series, line, inspection)
        )

    declared = [candidate for candidate in candidates if candidate.inspection.declared]
    if declared:
        return max(
            declared,
            key=lambda candidate: (
                *engine.playlist_quality_key(candidate.inspection.quality),
                -candidate.search_index,
                -candidate.line.number,
            ),
        )
    return (
        min(
            candidates,
            key=lambda candidate: (candidate.search_index, candidate.line.number),
        )
        if candidates
        else None
    )


def _source(source: str) -> engine.Source:
    if _supported_url(source):
        source = _validate_link(source)
        if "iyf.lv" in source:
            return engine.parse_input(source)
        return normalize.normalize_url(source)

    if source.lower().startswith(("http://", "https://")):
        raise engine.IyfError("link must be an http(s) iyf.tv or iyf.lv URL")
    if source.isdigit():
        return engine.parse_input(source)

    matches = query(source)
    if not matches:
        raise engine.IyfError(f"no matches for query {source!r}")
    selection = _pick_query_source(source, matches)
    return (
        selection.source if selection is not None else engine.Source(matches[0].show_id)
    )


def _select_episodes(
    available: list[engine.Episode], url_episode: int | None, selector: str | None
) -> list[engine.Episode]:
    by_number = {int(item.number): item for item in available}
    if selector is None:
        if url_episode is not None:
            chosen = by_number.get(url_episode)
            if chosen is None:
                raise engine.IyfError(f"episode {url_episode} not found on this line")
            return [chosen]
        return available[:1]

    selector = selector.strip().lower()
    if selector == "all":
        return available
    if not selector:
        raise engine.IyfError("episode selector must not be empty")

    numbers: list[int] = []
    for part in selector.split(","):
        part = part.strip()
        if not part:
            raise engine.IyfError(f"invalid episode selector {selector!r}")
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2 or not all(bound.isdigit() for bound in bounds):
                raise engine.IyfError(f"invalid episode range {part!r}")
            start, end = (int(bound) for bound in bounds)
            if start > end:
                raise engine.IyfError(f"episode range must ascend: {part!r}")
            numbers.extend(range(start, end + 1))
        elif part.isdigit():
            numbers.append(int(part))
        else:
            raise engine.IyfError(f"invalid episode selector {part!r}")

    selected: list[engine.Episode] = []
    for number in numbers:
        item = by_number.get(number)
        if item is None:
            raise engine.IyfError(f"episode {number} not found")
        if item not in selected:
            selected.append(item)
    return selected


def resolve_all(link_or_query: str, episode: str | None = None) -> list[Video]:
    """Resolve one or more videos from a URL or query."""
    source = link_or_query.strip()
    if not source:
        raise engine.IyfError("link or query must not be empty")
    normalized = _source(source)
    line_number = normalized.line or engine.DEFAULT_LINE
    series = engine.get_show(normalized.show_id)
    line = series.line(line_number)
    if line is None:
        available = [item.number for item in series.lines]
        raise engine.IyfError(f"line {line_number} not found (available: {available})")

    show_name = engine.RE_SEASON_TOKEN.sub("", series.title).strip() or series.title
    # A single-video entry (a film or documentary) is named like a movie, not
    # like an episode, so a media server does not file it under season 1.
    # Judge by the line being exported. Counting episode numbers across every
    # line makes a film look like a series whenever some other line happens to
    # split it into parts, even when the selected line holds it as one video.
    # A film offered by several lines that each hold one episode stays a film.
    is_series = len(line.episodes) > 1
    videos: list[Video] = []
    for selected_episode in _select_episodes(
        line.episodes, normalized.episode, episode
    ):
        play_info = engine.get_play_info(
            normalized.show_id, line.number, selected_episode.number
        )
        videos.append(
            Video(
                link=link_or_query,
                show_id=normalized.show_id,
                line=line.number,
                show_name=show_name,
                season=engine.season_from_title(series.title),
                episode=selected_episode.number,
                episode_title=selected_episode.title,
                media_class=play_info.media_class,
                stream_url=play_info.stream_url,
                is_series=is_series,
            )
        )
    return videos


def resolve(link_or_query: str, episode: str | None = None) -> Video:
    """Resolve one video; use ``resolve_all`` for batch selection."""
    videos = resolve_all(link_or_query, episode)
    if len(videos) != 1:
        raise engine.IyfError("more than one episode selected; use resolve_all")
    return videos[0]


def download(
    link_or_query: str,
    output: str | Path | None = None,
    episode: str | None = None,
    verbose: bool = False,
    concurrent_fragments: int = 8,
    progress: bool = True,
) -> list[Path]:
    """Download selected videos and return their output paths."""
    if concurrent_fragments < 1:
        raise engine.IyfError("concurrent_fragments must be at least 1")
    videos = resolve_all(link_or_query, episode)
    root = Path(output) if output is not None else Path("iyf_downloads")
    renderer = ProgressRenderer(videos) if progress else None
    paths: list[Path] = []
    try:
        for index, video in enumerate(videos):
            destination = library_path(root, video, video.filename)

            engine.download(
                video.stream_url,
                str(destination),
                verbose=verbose,
                concurrent_fragments=concurrent_fragments,
                progress_cb=renderer.callback(index) if renderer else None,
                show_progress=False,
            )
            if renderer:
                renderer.finish(index)
            paths.append(destination)
    finally:
        if renderer:
            renderer.close()
    return paths


SIDECAR_NAME = ".iyf.json"
"""File that records which show and line a stream directory came from."""

_EPISODE_RE = re.compile(r" S\d+E(\d+)")


def library_path(root: str | Path, video: Video, filename: str) -> Path:
    """Return the media-library path for one video.

    The layout is the convention shared by Jellyfin, Emby, Plex and Kodi:
    ``<root>/<title>/Season NN/<title> SxxExx.ext`` for series and
    ``<root>/<title>/<title>.ext`` for single videos. Nothing else about any
    media server is assumed.
    """
    directory = series_directory(root, video)
    if video.is_series:
        directory = directory / f"Season {video.season:02d}"
    return directory / filename


def series_directory(root: str | Path, video: Video) -> Path:
    """Return the directory that holds one title: its seasons and files."""
    return Path(root) / engine.sanitize_filename(video.show_name)


def _write_sidecar(directory: Path, video: Video) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"show_id": video.show_id, "line": video.line, "title": video.show_name}
    (directory / SIDECAR_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_sidecar(directory: Path) -> tuple[str, int, str] | None:
    """Return the ``(show_id, line, title)`` a directory's files came from."""
    try:
        payload = json.loads((directory / SIDECAR_NAME).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    show_id, line, title = (
        payload.get("show_id"),
        payload.get("line"),
        payload.get("title"),
    )
    if not isinstance(show_id, str) or not isinstance(line, int):
        return None
    if not isinstance(title, str) or not title.strip():
        # A title is what identifies the files this sidecar describes; without
        # one its directory cannot be filtered safely.
        return None
    return show_id, line, title


def _reject_mixed_sources(destinations: list[tuple[Path, Video]]) -> None:
    """Refuse to add files from another source to a directory that has one.

    Two iyf entries can carry the same title and season (49684 and 95676 are
    both 权力的游戏 第一季), so writing both into one folder would overwrite
    files and then let refresh re-resolve every file from whichever entry was
    written last.
    """
    for destination, video in destinations:
        existing = _read_sidecar(destination.parent)
        if existing is None:
            continue
        show_id, line, _title = existing
        if (show_id, line) != (video.show_id, video.line):
            raise engine.IyfError(
                f"{destination.parent} already holds streams from show {show_id} "
                f"line {line}; refusing to mix it with show {video.show_id} "
                f"line {video.line}. Delete that directory or its "
                f"{SIDECAR_NAME} to switch sources."
            )


def _belongs_to_title(stream_file: Path, title: str) -> bool:
    """Return whether a ``.strm`` file is one this sidecar describes.

    Only the exact single-video name or the ``<title> SxxExx`` shape counts, so
    an unrelated ``<title> Sideload.strm`` beside them is left alone.
    """
    safe = engine.sanitize_filename(title)
    stem = stream_file.stem
    return stem == safe or bool(re.fullmatch(rf"{re.escape(safe)} S\d+E\d+", stem))


def write_streams(
    link_or_query: str,
    output: str | Path | None = None,
    episode: str | None = None,
) -> list[Path]:
    """Write ``.strm`` pointer files instead of downloading media.

    Each file holds only the resolved HLS URL, which is what media servers
    read. The directory holding the files - the season folder for a series, the
    title folder for a single video - also gets an :data:`SIDECAR_NAME` file
    recording the show and line they were resolved from, so
    :func:`refresh_streams` can re-resolve later without searching by name
    again. Writing files from a different show or line into a directory that
    already holds one is refused instead of silently mixed.
    """
    videos = resolve_all(link_or_query, episode)
    root = Path(output) if output is not None else Path("iyf_downloads")
    planned = [
        (library_path(root, video, video.stream_filename), video) for video in videos
    ]
    _reject_mixed_sources(planned)
    paths: list[Path] = []
    for destination, video in planned:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"{video.stream_url}\n", encoding="utf-8")
        # The sidecar sits next to the files it describes: a season folder for
        # a series, the title folder for a single video. One title can hold
        # several seasons, so a single sidecar per title would let a later
        # season overwrite the show and line the other seasons refresh from.
        _write_sidecar(destination.parent, video)
        paths.append(destination)
    return paths


@dataclass
class StreamStatus:
    """What a refresh did with one ``.strm`` file."""

    path: Path
    status: str  # "ok", "updated" or "broken"
    detail: str = ""
    url: str | None = None


def _candidate_directories(target: Path) -> list[Path]:
    """Return the directories under ``target`` that may hold a sidecar."""
    if target.is_file():
        return [target.parent]
    if not target.is_dir():
        return []
    found = [target]
    for pattern in ("*", "*/*"):
        found.extend(sorted(item for item in target.glob(pattern) if item.is_dir()))
    return found


def _broken(directory: Path, detail: str, title: str = "") -> list[StreamStatus]:
    return [
        StreamStatus(item, "broken", detail)
        for item in sorted(directory.glob("*.strm"))
        if _belongs_to_title(item, title)
    ]


def refresh_streams(path: str | Path, check_only: bool = False) -> list[StreamStatus]:
    """Re-resolve the ``.strm`` files of one title and rewrite stale ones.

    Point it at a library root or a single title directory. Files are matched
    to episodes by the ``SxxExx`` part of their name; a file without one is
    treated as a single-video entry. With ``check_only`` nothing is rewritten,
    and only a broken file makes the exit status non-zero: one that merely
    needed updating is not an error.
    """
    statuses: list[StreamStatus] = []
    for directory in _candidate_directories(Path(path)):
        sidecar = _read_sidecar(directory)
        if sidecar is None:
            continue
        show_id, line_number, title = sidecar
        try:
            series = engine.get_show(show_id)
        except engine.IyfError as error:
            statuses.extend(_broken(directory, f"show {show_id}: {error}", title))
            continue
        line = series.line(line_number)
        if line is None:
            statuses.extend(_broken(directory, f"line {line_number} is gone", title))
            continue
        by_number = {int(item.number): item for item in line.episodes}
        for stream_file in sorted(directory.glob("*.strm")):
            if not _belongs_to_title(stream_file, title):
                # Another tool's pointer file: a sidecar claims this title's
                # files, not everything that happens to sit beside them.
                continue
            match = _EPISODE_RE.search(stream_file.stem)
            episode = int(match.group(1)) if match else min(by_number, default=None)
            if episode is None or episode not in by_number:
                statuses.append(StreamStatus(stream_file, "broken", "episode is gone"))
                continue
            try:
                play_info = engine.get_play_info(
                    show_id, line_number, by_number[episode].number
                )
                inspection = engine.inspect_playlist_url(play_info.stream_url)
                current = stream_file.read_text(encoding="utf-8").strip()
            except (engine.IyfError, OSError) as error:
                statuses.append(StreamStatus(stream_file, "broken", str(error)))
                continue
            if not inspection.valid:
                statuses.append(
                    StreamStatus(
                        stream_file,
                        "broken",
                        "playlist is not playable",
                        play_info.stream_url,
                    )
                )
                continue
            if current == play_info.stream_url:
                statuses.append(
                    StreamStatus(stream_file, "ok", "", play_info.stream_url)
                )
                continue
            if not check_only:
                stream_file.write_text(f"{play_info.stream_url}\n", encoding="utf-8")
            statuses.append(
                StreamStatus(
                    stream_file, "updated", "url changed", play_info.stream_url
                )
            )
    return statuses


def skill_text() -> str:
    """Return the packaged SKILL.md instructions."""
    return files("iyf").joinpath("SKILL.md").read_text(encoding="utf-8")


IyfError = engine.IyfError
__all__ = [
    "SIDECAR_NAME",
    "StreamStatus",
    "Video",
    "download",
    "library_path",
    "query",
    "refresh_streams",
    "resolve",
    "resolve_all",
    "series_directory",
    "skill_text",
    "write_streams",
    "IyfError",
]
