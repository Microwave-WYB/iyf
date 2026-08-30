"""Python API for downloading videos from iyf.tv and iyf.lv."""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import httpx

from . import engine, normalize
from .progress import ProgressRenderer

__version__ = "0.1.0"
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


def _source(source: str) -> engine.Source:
    if _supported_url(source):
        source = _validate_link(source)
        if "iyf.lv" in source:
            return engine.parse_input(source)
        return normalize.normalize_url(source)

    if source.lower().startswith(("http://", "https://")):
        raise engine.IyfError("link must be an http(s) iyf.tv or iyf.lv URL")
    matches = query(source)
    if not matches:
        raise engine.IyfError(f"no matches for query {source!r}")
    return engine.Source(matches[0].show_id)


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
