from iyf import _select_quality_line, _source, engine

DEAD_MEDIA = "#EXTM3U\n" + "\n".join(
    f"#EXTINF:0,\nsegment-{index}.ts" for index in range(100)
)
MEDIA = """#EXTM3U
#EXTINF:10.0,
segment-1.ts
#EXTINF:12.5,
segment-2.ts
"""
MASTER_720 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720
https://cdn.example/child.m3u8
"""
MASTER_1080 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3684000,RESOLUTION=1920x1080
https://cdn.example/child.m3u8
"""
MASTER_RESOLUTION_WINS = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1280x720
https://cdn.example/720-high-bw.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080
https://cdn.example/1080-low-bw.m3u8
"""
MASTER_FALLBACK = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080
https://cdn.example/high-fails.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720
https://cdn.example/low-works.m3u8
"""
MASTER_CHAIN_ROOT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080
https://cdn.example/chain-level-1.m3u8
"""
MASTER_CHAIN_LEVEL_1 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1920x1080
https://cdn.example/chain-media.m3u8
"""


def test_zero_duration_media_playlist_is_invalid_even_with_segments() -> None:
    inspection = engine.inspect_playlist(DEAD_MEDIA)

    assert not inspection.valid
    assert DEAD_MEDIA.count("segment-") == 100
    assert inspection.duration == 0
    assert not inspection.declared


def test_media_playlist_with_real_duration_is_valid_without_declared_quality() -> None:
    inspection = engine.inspect_playlist(MEDIA)

    assert inspection.valid
    assert inspection.duration == 22.5
    assert not inspection.declared


def test_master_with_404_child_playlist_is_invalid(monkeypatch) -> None:
    def fail(url: str) -> str:
        raise engine.IyfError("HTTP 404")

    monkeypatch.setattr(engine, "_http_get", fail)

    inspection = engine.inspect_playlist(MASTER_1080, "https://cdn.example/master.m3u8")

    assert not inspection.valid
    assert inspection.quality is not None
    assert inspection.quality.height == 1080


def test_master_with_zero_duration_child_playlist_is_invalid(monkeypatch) -> None:
    monkeypatch.setattr(engine, "_http_get", lambda url: DEAD_MEDIA)

    inspection = engine.inspect_playlist(MASTER_1080, "https://cdn.example/master.m3u8")

    assert not inspection.valid
    assert inspection.duration == 0


def test_master_variant_quality_prefers_resolution_before_bandwidth(
    monkeypatch,
) -> None:
    playlists = {
        "https://cdn.example/720-high-bw.m3u8": MEDIA,
        "https://cdn.example/1080-low-bw.m3u8": MEDIA,
    }
    monkeypatch.setattr(engine, "_http_get", playlists.__getitem__)

    inspection = engine.inspect_playlist(
        MASTER_RESOLUTION_WINS, "https://cdn.example/master.m3u8"
    )

    assert inspection.valid
    assert inspection.quality is not None
    assert engine.playlist_quality_key(inspection.quality) == (
        1080,
        1920,
        1000000,
        -1,
    )


def test_master_chain_reaches_media_playlist(monkeypatch) -> None:
    playlists = {
        "https://cdn.example/chain-level-1.m3u8": MASTER_CHAIN_LEVEL_1,
        "https://cdn.example/chain-media.m3u8": MEDIA,
    }
    monkeypatch.setattr(engine, "_http_get", playlists.__getitem__)

    inspection = engine.inspect_playlist(
        MASTER_CHAIN_ROOT, "https://cdn.example/master.m3u8"
    )

    assert inspection.valid
    assert inspection.duration == 22.5
    assert inspection.quality is not None
    assert inspection.quality.height == 1080


def test_master_child_fallback_uses_first_valid_variant(monkeypatch) -> None:
    def fetch(url: str) -> str:
        if url.endswith("high-fails.m3u8"):
            raise engine.IyfError("HTTP 404")
        return MEDIA

    monkeypatch.setattr(engine, "_http_get", fetch)

    inspection = engine.inspect_playlist(
        MASTER_FALLBACK, "https://cdn.example/master.m3u8"
    )

    assert inspection.valid
    assert inspection.quality is not None
    assert inspection.quality.height == 720
    assert inspection.quality.bandwidth == 1000000


def test_master_request_budget_caps_failed_variants(monkeypatch) -> None:
    root = "https://cdn.example/five-failures.m3u8"
    children = [f"https://cdn.example/failure-{index}.m3u8" for index in range(5)]
    master = "#EXTM3U\n" + "\n".join(
        f"#EXT-X-STREAM-INF:BANDWIDTH={index + 1},RESOLUTION=1920x1080\n{url}"
        for index, url in enumerate(children)
    )
    requests: list[str] = []

    def fetch(url: str) -> str:
        requests.append(url)
        if url == root:
            return master
        raise engine.IyfError("HTTP 404")

    monkeypatch.setattr(engine, "_http_get", fetch)

    inspection = engine.inspect_playlist_url(root)

    assert not inspection.valid
    assert len(requests) == 5


def test_master_self_cycle_requests_url_once(monkeypatch) -> None:
    root = "https://cdn.example/self-cycle.m3u8"
    master = (
        f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080\n{root}\n"
    )
    requests: list[str] = []

    def fetch(url: str) -> str:
        requests.append(url)
        return master

    monkeypatch.setattr(engine, "_http_get", fetch)

    inspection = engine.inspect_playlist_url(root)

    assert not inspection.valid
    assert requests == [root]


def test_identical_declared_tags_choose_lower_line_number(monkeypatch) -> None:
    series = engine.Series(
        "95676",
        "权力的游戏 第一季",
        [
            engine.Line(3, [engine.Episode("1", "第一集")]),
            engine.Line(2, [engine.Episode("1", "第一集")]),
        ],
    )
    urls = {2: "https://cdn.example/line-2", 3: "https://cdn.example/line-3"}
    playlists = {
        urls[2]: MASTER_1080,
        urls[3]: MASTER_1080,
        "https://cdn.example/child.m3u8": MEDIA,
    }

    monkeypatch.setattr(
        engine,
        "get_play_info",
        lambda show_id, line, episode: engine.PlayInfo(urls[line]),
    )
    monkeypatch.setattr(engine, "_http_get", playlists.__getitem__)

    choice = _select_quality_line(series)

    assert choice is not None
    assert choice[0].number == 2
    assert choice[1].quality is not None
    assert choice[1].quality.width == 1920
    assert choice[1].quality.height == 1080
    assert choice[1].quality.bandwidth == 3684000


def test_quality_line_inspection_is_capped(monkeypatch) -> None:
    series = engine.Series(
        "many-lines",
        "Many lines",
        [engine.Line(index, [engine.Episode("1", "第一集")]) for index in range(1, 11)],
    )
    inspected: list[int] = []

    def inspect(show_id: str, line: engine.Line) -> engine.PlaylistInspection:
        inspected.append(line.number)
        return engine.PlaylistInspection(
            True,
            engine.PlaylistQuality(width=1280, height=720, bandwidth=1200000),
            10,
        )

    monkeypatch.setattr("iyf._inspect_line", inspect)

    choice = _select_quality_line(series)

    assert choice is not None
    assert inspected == [1, 2, 3, 4, 5, 6]


def test_query_compares_quality_across_search_matches(monkeypatch) -> None:
    first = engine.Series(
        "720-show",
        "First result",
        [engine.Line(1, [engine.Episode("1", "第一集")])],
    )
    second = engine.Series(
        "1080-show",
        "Second result",
        [engine.Line(1, [engine.Episode("1", "第一集")])],
    )
    results = [
        engine.SearchResult(first.show_id, first.title),
        engine.SearchResult(second.show_id, second.title),
    ]
    urls = {
        (first.show_id, 1): "https://cdn.example/720",
        (second.show_id, 1): "https://cdn.example/1080",
    }
    playlists = {
        urls[(first.show_id, 1)]: MASTER_720,
        urls[(second.show_id, 1)]: MASTER_1080,
        "https://cdn.example/child.m3u8": MEDIA,
    }

    monkeypatch.setattr("iyf.query", lambda text: results)
    monkeypatch.setattr(
        engine,
        "get_show",
        lambda show_id: {first.show_id: first, second.show_id: second}[show_id],
    )
    monkeypatch.setattr(
        engine,
        "get_play_info",
        lambda show_id, line, episode: engine.PlayInfo(urls[(show_id, line)]),
    )
    monkeypatch.setattr(engine, "_http_get", playlists.__getitem__)

    source = _source("same query")

    assert source.show_id == second.show_id
    assert source.line == 1


def test_query_skips_valid_but_undeclared_match_until_declared_quality(
    monkeypatch,
) -> None:
    first = engine.Series(
        "49684",
        "权力的游戏第一季",
        [
            engine.Line(1, [engine.Episode("1", "第一集")]),
            engine.Line(2, [engine.Episode("1", "第一集")]),
        ],
    )
    second = engine.Series(
        "95676",
        "权力的游戏 第一季",
        [engine.Line(2, [engine.Episode("1", "第一集")])],
    )
    results = [
        engine.SearchResult("49684", first.title),
        engine.SearchResult("95676", second.title),
    ]
    urls = {
        ("49684", 1): "https://cdn.example/dead",
        ("49684", 2): "https://cdn.example/zero-duration",
        ("95676", 2): "https://cdn.example/1080",
    }
    playlists = {
        urls[("49684", 1)]: MEDIA,
        urls[("49684", 2)]: DEAD_MEDIA,
        urls[("95676", 2)]: MASTER_1080,
        "https://cdn.example/child.m3u8": MEDIA,
    }

    monkeypatch.setattr("iyf.query", lambda text: results)
    monkeypatch.setattr(
        engine,
        "get_show",
        lambda show_id: {"49684": first, "95676": second}[show_id],
    )
    monkeypatch.setattr(
        engine,
        "get_play_info",
        lambda show_id, line, episode: engine.PlayInfo(urls[(show_id, line)]),
    )
    monkeypatch.setattr(engine, "_http_get", playlists.__getitem__)

    source = _source("权力的游戏 第一季")

    assert source.show_id == "95676"
    assert source.line == 2
