"""Typer command-line interface for the :mod:`iyf` library."""

import json
from pathlib import Path

import typer
from rich.console import Console

from . import (
    IyfError,
    _pick_query_source,
    _select_quality_line,
    _source,
    engine,
    refresh_streams,
    skill_text,
    write_streams,
)
from . import download as download_video
from . import query as search_shows
from .render import Candidate, candidates_table, episodes_table, search_results_table

app = typer.Typer(
    name="iyf",
    help="下载 iyf.tv 和 iyf.lv 视频。",
    invoke_without_command=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()


def _download(
    source: str,
    output: Path | None,
    episode: str | None,
    verbose: bool,
    concurrent_fragments: int,
    json_output: bool,
    selection_message: str | None = None,
    strm: bool = False,
) -> None:
    resolved_source = source
    try:
        if (
            not json_output
            and not source.lower().startswith(("http://", "https://"))
            and not source.strip().isdigit()
        ):
            selection = _pick_query_source(source)
            if selection is not None:
                resolved_source = (
                    f"https://www.iyf.lv/iyfplay/"
                    f"{selection.result.show_id}-{selection.line.number}-1/"
                )
                selection_message = (
                    f"已选择：{selection.result.title} 线路 "
                    f"{selection.line.number}（"
                    f"{engine.playlist_quality_label(selection.inspection)}）"
                )
        # A link or a bare show id can leave the line unspecified too, and then
        # the export silently falls back to DEFAULT_LINE, which may be a line
        # whose episodes are all gone. Pick a line the same way a title query
        # does, unless the caller already pinned one in the url.
        if not json_output and (
            resolved_source.lower().startswith(("http://", "https://"))
            or resolved_source.strip().isdigit()
        ):
            parsed = _source(resolved_source)
            if parsed.line is None:
                series = engine.get_show(parsed.show_id)
                choice = _select_quality_line(series)
                if choice is not None:
                    line, inspection = choice
                    resolved_source = (
                        f"https://www.iyf.lv/iyfplay/{parsed.show_id}-{line.number}-1/"
                    )
                    selection_message = (
                        f"已选择：{series.title} 线路 {line.number}（"
                        f"{engine.playlist_quality_label(inspection)}）"
                    )
        if not json_output:
            console.print(
                selection_message or "正在请求 iyf API 并解析视频信息，请稍候…",
                style="dim",
            )
        if strm:
            destinations = write_streams(resolved_source, output, episode)
        else:
            destinations = download_video(
                resolved_source,
                output,
                episode,
                verbose=verbose and not json_output,
                concurrent_fragments=concurrent_fragments,
                progress=not (json_output or verbose),
            )
    except IyfError as error:
        typer.echo(f"错误：{error}", err=True)
        raise typer.Exit(1) from error
    except KeyboardInterrupt:
        typer.echo("\n已中断", err=True)
        raise typer.Exit(130) from None
    if json_output:
        typer.echo(
            json.dumps(
                [{"path": str(destination)} for destination in destinations],
                ensure_ascii=False,
            )
        )
    else:
        for destination in destinations:
            typer.echo(f"{'已写入' if strm else '已保存到'}：{destination}")


def _interactive(verbose: bool = False) -> None:
    text = typer.prompt("搜索剧集/电影").strip()
    if not text:
        typer.echo("查询不能为空。", err=True)
        raise typer.Exit(1)

    console.print("正在请求 iyf API 搜索，请稍候…", style="dim")
    try:
        matches = search_shows(text)
    except IyfError as error:
        typer.echo(f"错误：{error}", err=True)
        raise typer.Exit(1) from error
    if not matches:
        typer.echo("没有找到匹配项。")
        return

    candidates: list[Candidate] = []
    # Existing behavior: enumerate every match for the candidate table; this
    # one show/play lookup per match is outside the quality-selection limits.
    for result in matches:
        media_class = "视频"
        try:
            series = engine.get_show(result.show_id)
            line = series.line(engine.DEFAULT_LINE) or (
                series.lines[0] if series.lines else None
            )
            if line and line.episodes:
                media_class = (
                    engine.get_play_info(
                        result.show_id, line.number, line.episodes[0].number
                    ).media_class
                    or media_class
                )
        except IyfError:
            series = engine.Series(result.show_id, result.title, [])
            line = None
        candidates.append(Candidate(result, series, line, media_class))

    console.print(candidates_table(candidates))
    target = typer.prompt("选择编号", type=int, default=1)
    if not 1 <= target <= len(candidates):
        typer.echo("编号无效。", err=True)
        raise typer.Exit(1)

    candidate = candidates[target - 1]
    selection_message = None
    quality_choice = _select_quality_line(candidate.series)
    if quality_choice is not None:
        quality_line = quality_choice[0]
        candidate = Candidate(
            candidate.result,
            candidate.series,
            quality_choice[0],
            candidate.media_class,
        )
        selection_message = (
            f"已选择：{candidate.series.title} 线路 {quality_line.number}（"
            f"{engine.playlist_quality_label(quality_choice[1])}）"
        )
    if candidate.line:
        source = (
            f"https://www.iyf.lv/iyfplay/"
            f"{candidate.result.show_id}-{candidate.line.number}-1/"
        )
    else:
        source = f"https://www.iyf.lv/iyftv/{candidate.result.show_id}/"
    if candidate.line and candidate.line.episodes:
        console.print(episodes_table(candidate.line))
        typer.echo("分集选择示例：")
        typer.echo("  all        下载全部")
        typer.echo("  3-12       下载第 3 到 12 集")
        typer.echo("  7-9        下载第 7 到 9 集")
        selector = typer.prompt("输入分集", default="all")
    else:
        selector = None
    _download(source, None, selector, verbose, 8, False, selection_message)


@app.callback()
def main(
    ctx: typer.Context,
    skill: bool = typer.Option(
        False,
        "--skill",
        help="Print LLM agent skill",
    ),
    verbose: bool = typer.Option(
        False,
        "-V",
        "--verbose",
        help="显示 yt-dlp 详细日志。",
    ),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if skill:
        typer.echo(skill_text(), nl=False)
        raise typer.Exit
    if ctx.invoked_subcommand is None:
        _interactive(verbose)


@app.command("d")
@app.command("download")
def download(
    ctx: typer.Context,
    link_or_query: str = typer.Argument(
        ..., help="直接复制的 iyf.tv/iyf.lv 链接，或剧名查询"
    ),
    output: Path | None = typer.Option(
        None,
        "-o",
        help="媒体库根目录（默认：iyf_downloads/）",
    ),
    episode: str | None = typer.Option(
        None,
        "-e",
        "--episode",
        help='分集选择："all"、"1-24" 或 "1,3-5,23"。',
    ),
    verbose: bool = typer.Option(
        False,
        "-V",
        "--verbose",
        help="显示 yt-dlp 详细日志。",
    ),
    concurrent_fragments: int = typer.Option(
        8,
        "-N",
        "--concurrent-fragments",
        min=1,
        help="每个视频同时下载的 HLS 分片数（默认：8）。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="以 JSON 输出结果。",
    ),
    strm: bool = typer.Option(
        False,
        "--strm",
        help="写入 .strm 流文件（指向 HLS 地址）而不下载视频。",
    ),
) -> None:
    """解析并下载一个或多个视频，或用 --strm 写入流文件。"""
    if isinstance(ctx.obj, dict) and ctx.obj.get("verbose") is True:
        verbose = True
    _download(
        link_or_query,
        output,
        episode,
        verbose,
        concurrent_fragments,
        json_output,
        strm=strm,
    )


@app.command("q")
@app.command("query")
def query(
    text: str = typer.Argument(..., help="要搜索的剧名"),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="以 JSON 输出结果。",
    ),
) -> None:
    """搜索 iyf.lv 并显示匹配的剧集。"""
    if not json_output:
        console.print("正在请求 iyf API 搜索，请稍候…", style="dim")
    try:
        matches = search_shows(text)
    except IyfError as error:
        typer.echo(f"错误：{error}", err=True)
        raise typer.Exit(1) from error
    if not matches:
        if json_output:
            typer.echo("[]")
        else:
            typer.echo("没有找到匹配项。")
        return
    if json_output:
        typer.echo(
            json.dumps(
                [{"show_id": match.show_id, "title": match.title} for match in matches],
                ensure_ascii=False,
            )
        )
    else:
        console.print(search_results_table(matches))


@app.command("refresh")
def refresh(
    path: Path = typer.Argument(..., help="媒体库根目录或单个剧集目录"),
    check_only: bool = typer.Option(
        False,
        "--check-only",
        help="只检查是否需要刷新，不重写文件。",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="以 JSON 输出结果。",
    ),
) -> None:
    """重新解析已有 .strm 文件，重写过期的地址。"""
    if not json_output:
        console.print("正在重新解析流地址，请稍候…", style="dim")
    try:
        statuses = refresh_streams(path, check_only=check_only)
    except IyfError as error:
        typer.echo(f"错误：{error}", err=True)
        raise typer.Exit(1) from error
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "path": str(item.path),
                        "status": item.status,
                        "detail": item.detail,
                    }
                    for item in statuses
                ],
                ensure_ascii=False,
            )
        )
    else:
        if not statuses:
            typer.echo("没有找到带 .iyf.json 的目录，未做任何修改。")
        labels = {"ok": "正常", "updated": "已更新", "broken": "失效"}
        for item in statuses:
            suffix = f"（{item.detail}）" if item.detail else ""
            typer.echo(f"{labels.get(item.status, item.status)}：{item.path}{suffix}")
    if any(item.status == "broken" for item in statuses):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
