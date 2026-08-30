"""Pure Rich renderable builders for the interactive CLI."""

from dataclasses import dataclass

from rich.table import Table
from rich.text import Text

from .engine import Line, SearchResult, Series


@dataclass
class Candidate:
    """A selectable search result with its resolved metadata."""

    result: SearchResult
    series: Series
    line: Line | None
    media_class: str


def candidates_table(candidates: list[Candidate]) -> Table:
    """Build a table of selectable series and movies without printing it."""
    table = Table()
    table.add_column("编号", justify="right", style="cyan")
    table.add_column("类型", style="magenta")
    table.add_column("名称", style="green")
    for index, candidate in enumerate(candidates, 1):
        table.add_row(
            str(index), Text(f"[{candidate.media_class}]"), candidate.series.title
        )
    return table


def episodes_table(line: Line) -> Table:
    """Build one row per episode without printing it."""
    table = Table()
    table.add_column("分集", justify="right", style="cyan")
    table.add_column("标题", style="green")
    for episode in line.episodes:
        table.add_row(episode.number, episode.title)
    return table


def search_results_table(matches: list[SearchResult]) -> Table:
    """Build a search-results table without printing it."""
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("名称", style="green")
    for match in matches:
        table.add_row(match.show_id, match.title)
    return table
