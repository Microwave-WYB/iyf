"""Python API for downloading videos from iyf.tv and iyf.lv."""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import httpx

from . import engine, normalize
from .progress import ProgressRenderer

__version__ = "0.2.0"
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

    @property
    def filename(self) -> str:
        """Return a filesystem-safe filename based on the resolved video name."""
        return engine.sanitize_filename(
            f"{self.show_name} S{self.season:02d}E{int(self.episode):02d}.mp4"
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
    output_path = Path(output) if output is not None else None
    renderer = ProgressRenderer(videos) if progress else None
    paths: list[Path] = []
    try:
        for index, video in enumerate(videos):
            if output_path is None:
                destination = (
                    Path("iyf_downloads")
                    / engine.sanitize_filename(video.show_name)
                    / video.filename
                )
            else:
                destination = output_path / video.filename

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


def skill_text() -> str:
    """Return the packaged SKILL.md instructions."""
    return files("iyf").joinpath("SKILL.md").read_text(encoding="utf-8")


IyfError = engine.IyfError
__all__ = [
    "Video",
    "download",
    "query",
    "resolve",
    "resolve_all",
    "skill_text",
    "IyfError",
]
