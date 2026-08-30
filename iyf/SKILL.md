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
  {"path": "iyf_downloads/生活大爆炸/生活大爆炸 S04E02.mp4"}
]
```

Each item has `path: str`, the downloaded file path.

The `-o` option always specifies an output directory. Filenames are generated automatically; do not pass a file path to `-o`.

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

    @property
    def filename(self) -> str: ...
```

### Function signatures

```python
from pathlib import Path
from iyf import download, query, resolve, resolve_all


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
```

### Usage

```python
matches = query("生活大爆炸")
video = resolve("https://www.iyf.lv/iyfplay/49615-1-2/")
video_list = resolve_all("剧名", episode="1-3")
paths = download("剧名", episode="1-3")
```

`resolve()` returns one `Video` and raises if multiple episodes are selected. Use `resolve_all()` for multiple episodes. Default output is `iyf_downloads/<series-or-movie>/<video>.mp4`.
