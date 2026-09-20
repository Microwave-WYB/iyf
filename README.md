# iyf

## CLI
```sh
uv tool install git+https://github.com/Microwave-WYB/iyf
iyf
```
## Let your LLM agent explain everything to you:

Tell your agent to run this:
```
iyf --skill
```


## Library
```
# In a uv project:
uv add git+https://github.com/Microwave-WYB/iyf.git

# Or, using pip
pip install git+https://github.com/Microwave-WYB/iyf.git
```

```python
import iyf

matches = iyf.query("生活大爆炸")
video = iyf.resolve("https://www.iyf.lv/iyfplay/49615-1-2/")
print(video.filename)
paths = iyf.download(video.link, episode="1-3")
```

<!--Minimal Python library and CLI for downloading videos from [iyf.tv](https://www.iyf.tv)
and [iyf.lv](https://www.iyf.lv). It resolves a copied page URL or show-name
query, finds the embedded HLS stream, and downloads it with yt-dlp.

## Install

CLI only:

```sh
uv tool install https://github.com/Microwave-WYB/iyf
```

Library (inside your virtual environment):

```sh
uv add iyf
# or
pip install iyf
```

## CLI

```sh
iyf download <link-or-query>
iyf download <link-or-query> -o /path/to/output-directory
iyf d "show name" -e all
iyf d "show name" -e "1-24"
iyf d "show name" -e "1,3-5,23"
iyf d "show name" -e 1 -V
iyf d "show name" -e all -N 8
iyf q "show name"
iyf --skill
```

Non-interactive commands support `--json`:

```sh
iyf q "show name" --json
iyf d "show name" -e all --json
```

Search JSON is a list of `{ "show_id": ..., "title": ... }` objects; download JSON is a list of `{ "path": ... }` objects. Rich progress and yt-dlp logs are disabled in JSON mode.


Name-query quality selection is bounded to the first 3 search results and first 6 lines per result. Within that bounded set it selects the highest declared-quality valid HLS line. Each inspected line makes at most 5 playlist requests (1 root plus up to 4 expansions), so the 18-line cap allows at most 90 playlist requests. If a master has more variants than the remaining budget, only the highest-ranked remaining variants are tried. The interactive candidate table still enumerates all search results (existing behavior, outside this quality-selection cap). Identical quality tags use the lower line number, and explicit iyfplay/iyftv URLs and numeric IDs remain pinned instead of being upgraded. Child playlists use the fixed User-Agent/Referer; a production CDN requiring extra headers, cookies, or signed child URIs may make that line appear invalid. This is metadata selection only; it does not sample media with ffmpeg.

When `-o` is omitted, files are saved under
`iyf_downloads/<resolved series-or-movie name>/`. When provided, `-o` is always
treated as an output directory; iyf generates each filename automatically.

`-e`/`--episode` accepts `all`, an inclusive range such as `1-24`, or
comma-separated values and ranges such as `1,3-5,23`.
yt-dlp logs are hidden by default; use `-V`/`--verbose` to show them.
`-N`/`--concurrent-fragments` defaults to 8 and controls yt-dlp HLS fragment concurrency.

Running `iyf` without a command starts the interactive CLI: enter a query,
choose a numbered candidate, then enter `all` or an episode selection expression.

## Library

```python
from iyf import download, query, resolve

matches = query("The Big Bang Theory")
print(matches)
video = resolve("https://www.iyf.lv/iyfplay/49615-1-2/")
print(video.filename)
path = download(video.link, "video.mp4")
```

The public API consists of `iyf.Video`, `iyf.query(text)`,
`iyf.resolve(link_or_query)`, `iyf.download(link_or_query, output=None)`, and
`iyf.skill_text()`.-->
