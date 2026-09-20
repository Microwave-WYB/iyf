"""CLI 输入处理：链接与编号输入也必须挑一条可用线路。

两条曾经真出问题的行为写成了回归测试：

* 空白线路的链接/编号输入过去静默使用 DEFAULT_LINE，而线路 1 可能整季失效；
* 重写链接时把集数写成 1，而 url 里可能带着别的集数（iyf.tv 就带）。

另外 --json 只应改变打印内容，不应改变导出哪条流。
"""

from __future__ import annotations

import pathlib

import pytest
import typer

from iyf import cli, engine

MEDIA = """#EXTM3U
#EXTINF:10.0,
segment-1.ts
"""


def _series(*lines: engine.Line) -> engine.Series:
    return engine.Series("49676", "老友记 第一季", list(lines))


def _run(
    monkeypatch,
    tmp_path: pathlib.Path,
    source: str,
    parsed: engine.Source,
    series: engine.Series,
    choice,
    *,
    json_output: bool = False,
    episode: str | None = None,
) -> str:
    """跑一次 _download，返回真正交给 write_streams 的源地址。"""
    seen: dict[str, str] = {}
    monkeypatch.setattr(cli, "_source", lambda text: parsed)
    monkeypatch.setattr(engine, "get_show", lambda show_id: series)
    monkeypatch.setattr(cli, "_select_quality_line", lambda value: choice)
    monkeypatch.setattr(
        cli,
        "write_streams",
        lambda src, out, ep: (seen.setdefault("src", src), [tmp_path])[1],
    )
    cli._download(source, tmp_path, episode, False, 8, json_output, strm=True)
    return seen["src"]


def _one_episode_line(number: int) -> engine.Line:
    return engine.Line(number, [engine.Episode("1", "第1集")])


def test_a_link_without_a_line_gets_one_picked(monkeypatch, tmp_path) -> None:
    line = _one_episode_line(2)
    source = "https://www.iyf.lv/iyftv/49676/"

    resolved = _run(
        monkeypatch,
        tmp_path,
        source,
        engine.Source("49676"),
        _series(_one_episode_line(1), line),
        (line, engine.inspect_playlist(MEDIA)),
    )

    assert resolved == "https://www.iyf.lv/iyfplay/49676-2-1/"


def test_a_bare_show_id_gets_a_line_picked(monkeypatch, tmp_path) -> None:
    line = _one_episode_line(3)

    resolved = _run(
        monkeypatch,
        tmp_path,
        "49676",
        engine.Source("49676"),
        _series(_one_episode_line(1), line),
        (line, engine.inspect_playlist(MEDIA)),
    )

    assert resolved == "https://www.iyf.lv/iyfplay/49676-3-1/"


def test_json_output_still_picks_a_line(monkeypatch, tmp_path) -> None:
    # --json 改变的是打印内容，不是导出哪条流。
    line = _one_episode_line(2)

    resolved = _run(
        monkeypatch,
        tmp_path,
        "https://www.iyf.lv/iyftv/49676/",
        engine.Source("49676"),
        _series(_one_episode_line(1), line),
        (line, engine.inspect_playlist(MEDIA)),
        json_output=True,
    )

    assert resolved == "https://www.iyf.lv/iyfplay/49676-2-1/"


def test_a_url_episode_survives_line_selection(monkeypatch, tmp_path) -> None:
    # iyf.tv 这类链接可以只带集数：第 7 集不能被改成第 1 集。
    line = _one_episode_line(2)

    resolved = _run(
        monkeypatch,
        tmp_path,
        "https://www.iyf.tv/play/Abcd1234?episode=7",
        engine.Source("49676", None, 7),
        _series(_one_episode_line(1), line),
        (line, engine.inspect_playlist(MEDIA)),
    )

    assert resolved == "https://www.iyf.lv/iyfplay/49676-2-7/"


def test_a_pinned_line_is_left_alone(monkeypatch, tmp_path) -> None:
    picked: list[object] = []
    seen: dict[str, str] = {}
    monkeypatch.setattr(cli, "_source", lambda text: engine.Source("49676", 1, 1))
    monkeypatch.setattr(cli, "_select_quality_line", picked.append)
    monkeypatch.setattr(
        cli,
        "write_streams",
        lambda src, out, ep: (seen.setdefault("src", src), [tmp_path])[1],
    )

    cli._download(
        "https://www.iyf.lv/iyfplay/49676-1-1/",
        tmp_path,
        None,
        False,
        8,
        False,
        strm=True,
    )

    assert picked == []
    assert seen["src"] == "https://www.iyf.lv/iyfplay/49676-1-1/"


def test_no_playable_line_leaves_the_source_alone(monkeypatch, tmp_path) -> None:
    source = "https://www.iyf.lv/iyftv/49676/"

    resolved = _run(
        monkeypatch,
        tmp_path,
        source,
        engine.Source("49676"),
        _series(_one_episode_line(1)),
        None,
    )

    assert resolved == source


def test_a_foreign_link_still_fails(monkeypatch, tmp_path) -> None:
    def reject(text: str) -> engine.Source:
        raise engine.IyfError("unsupported link")

    monkeypatch.setattr(cli, "_source", reject)

    with pytest.raises(typer.Exit):
        cli._download(
            "https://example.com/whatever.m3u8",
            tmp_path,
            None,
            False,
            8,
            False,
            strm=True,
        )
