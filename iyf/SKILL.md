# iyf API skill

Use `iyf` to resolve and download videos from iyf.tv and iyf.lv.

## JSON API

Non-interactive `query/q` and `download/d` commands support `--json`.
JSON is written to stdout. Errors are written to stderr. In JSON mode, progress and yt-dlp logs are disabled.

### Search

```sh
iyf q "生活大爆炸" --json
```

Output:

```json
[
  {"show_id": "49615", "title": "生活大爆炸 第四季"},
  {"show_id": "49616", "title": "生活大爆炸 第三季"}
]
```

Each result has:

- `show_id: str`: iyf.lv show identifier
- `title: str`: display title

### Download

```sh
iyf d "https://www.iyf.lv/iyfplay/49615-1-2/" --json
iyf d "剧名" -e "1-3,5,6-9" --json
```

Output:

```json
[
  {"path": "iyf_downloads/生活大爆炸/Season 04/生活大爆炸 S04E02.mp4"}
]
```

Each item has `path: str`, the written file path.

Files use the media-library layout that Jellyfin, Emby, Plex and Kodi all read
(a directory convention, not an integration):
`<root>/<title>/Season NN/<title> SxxExx.mp4`, or `<root>/<title>/<title>.mp4`
for a single-video entry (a film or documentary). `-o` replaces the
`iyf_downloads/` root and the layout below it stays the same, so
`-o /mnt/storage/media` yields `/mnt/storage/media/生活大爆炸/Season 07/…`. Do not
pass a file path to `-o`.

`--strm` writes pointer files instead of downloading media, using the same
layout with the `.strm` extension:

```sh
iyf d "剧名" -e all --strm -o /mnt/storage/media
```

Each `.strm` file holds only the resolved HLS URL, and the directory holding the
files (the season folder for a series) gets an `.iyf.json` sidecar recording the
show and line it was resolved from. Each season keeps its own sidecar, because
the seasons of one title share a title directory.

### Refresh

```sh
iyf refresh /mnt/storage/media --check-only --json
```

Re-resolves the `.strm` files under a library root or a single title directory,
rewriting the ones whose URL changed. `--check-only` reports without writing
anything. Both forms exit non-zero only when a file is broken (show, line,
episode or playlist gone); a file that merely needed updating is not an error.

Known limitations: an entry with a single episode in total is laid out as a film
(there is no reliable media-type field); a directory holds one source at a time,
so writing files from another show or line into it is refused rather than mixed
(delete the directory or its `.iyf.json` to switch); and refresh only touches
`.strm` files whose names match the sidecar's title, leaving everything else,
including directories without a readable sidecar, alone.

Output:

```json
[
  {
    "path": "/mnt/storage/media/权力的游戏/Season 01/权力的游戏 S01E01.strm",
    "status": "ok",
    "detail": ""
  }
]
```

`status` is `ok`, `updated` or `broken`.

Episode selectors accepted by `-e`/`--episode`:

- `all`: all episodes
- `3-12`: inclusive range
- `1-3,5,6-9`: multiple ranges and individual episodes

## Python API

### Types

```python
from pathlib import Path
from dataclasses import dataclass


@dataclass
class SearchResult:
    show_id: str
    title: str


@dataclass
class Video:
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
        """mp4 filename: ``<title> SxxExx.mp4``, or ``<title>.mp4``."""

    @property
    def stream_filename(self) -> str:
        """Same name with the ``.strm`` extension."""


@dataclass
class StreamStatus:
    path: Path
    status: str  # "ok", "updated" or "broken"
    detail: str = ""
    url: str | None = None

    @property
    def filename(self) -> str: ...
```

### Function signatures

```python
from pathlib import Path
from iyf import (
    SIDECAR_NAME,
    StreamStatus,
    Video,
    download,
    library_path,
    query,
    refresh_streams,
    resolve,
    resolve_all,
    series_directory,
    write_streams,
)


def query(text: str) -> list[SearchResult]: ...


def resolve(
    link_or_query: str,
    episode: str | None = None,
) -> Video: ...


def resolve_all(
    link_or_query: str,
    episode: str | None = None,
) -> list[Video]: ...


def download(
    link_or_query: str,
    output: str | Path | None = None,
    episode: str | None = None,
    verbose: bool = False,
    concurrent_fragments: int = 8,
    progress: bool = True,
) -> list[Path]: ...


def write_streams(
    link_or_query: str,
    output: str | Path | None = None,
    episode: str | None = None,
) -> list[Path]: ...


def refresh_streams(
    path: str | Path,
    check_only: bool = False,
) -> list[StreamStatus]: ...


def library_path(root: str | Path, video: Video, filename: str) -> Path: ...


def series_directory(root: str | Path, video: Video) -> Path: ...
```

### Usage

```python
matches = query("生活大爆炸")
video = resolve("https://www.iyf.lv/iyfplay/49615-1-2/")
video_list = resolve_all("剧名", episode="1-3")
paths = download("剧名", episode="1-3")
streams = write_streams("剧名", episode="1-3", output="/mnt/storage/media")
statuses = refresh_streams("/mnt/storage/media", check_only=True)
```

`resolve()` returns one `Video` and raises if multiple episodes are selected. Use `resolve_all()` for multiple episodes. Files land in `iyf_downloads/<title>/Season NN/` (or `<title>/` for a single-video entry) unless `output` replaces that root; `library_path()` and `series_directory()` expose the same layout to callers that need to compute paths.

Name-query quality selection is bounded to the first 3 search results and first 6 lines per result. Within that bounded set it chooses the highest declared-quality valid HLS line. Each inspected line makes at most 5 playlist requests (1 root plus up to 4 expansions), so the 18-line cap allows at most 90 playlist requests. If a master has more variants than the remaining budget, only the highest-ranked remaining variants are tried. The interactive candidate table still enumerates all search results (existing behavior, outside this quality-selection cap). Equal tags are tied by lower line number; tags do not reveal undeclared frame-rate differences. Explicit iyfplay/iyftv URLs and numeric IDs remain pinned. Child playlists use the fixed User-Agent/Referer; a production CDN requiring extra headers, cookies, or signed child URIs may make that line appear invalid. No media quality probing is performed.
