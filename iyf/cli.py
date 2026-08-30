"""Typer command-line interface for the :mod:`iyf` library."""

import json
from pathlib import Path

import typer
from rich.console import Console

from . import IyfError, engine, skill_text
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
) -> None:
    if not json_output:
        console.print("正在请求 iyf API 并解析视频信息，请稍候…", style="dim")
    try:
        destinations = download_video(
            source,
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
            typer.echo(f"已保存到：{destination}")


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
    _download(source, None, selector, verbose, 8, False)


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
        help="输出目录（默认：iyf_downloads/<名称>/）",
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
) -> None:
    """解析并下载一个或多个视频。"""
    if isinstance(ctx.obj, dict) and ctx.obj.get("verbose") is True:
        verbose = True
    _download(
        link_or_query, output, episode, verbose, concurrent_fragments, json_output
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


if __name__ == "__main__":
    app()
