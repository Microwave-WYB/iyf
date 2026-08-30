"""Queued Rich progress rendering for multi-episode downloads."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

if TYPE_CHECKING:
    from . import Video


@dataclass
class ProgressEvent:
    """A progress update for one selected episode."""

    episode_index: int
    percent: float
    finished: bool = False


def _render(videos: list[Video], events: queue.Queue[ProgressEvent | None]) -> None:
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>5.1f}%"),
        TimeRemainingColumn(),
    )
    with progress:
        first_video = videos[0]
        current_id = progress.add_task(
            first_video.episode_title, total=100.0, visible=False
        )
        overall_id = progress.add_task("总进度", total=len(videos))
        current_visible = False
        warning_shown = False
        while True:
            event = events.get()
            if event is None:
                return
            video = videos[event.episode_index]
            if 0.0 < event.percent < 100.0:
                if not current_visible:
                    progress.update(current_id, visible=True)
                    current_visible = True
                progress.update(
                    current_id,
                    description=video.episode_title,
                    completed=event.percent,
                )
            elif event.percent >= 100.0 and not current_visible and not warning_shown:
                progress.console.print(
                    "当前分集无法获取实时进度，仅显示总进度。", style="yellow"
                )
                warning_shown = True
            if event.finished:
                if current_visible:
                    progress.update(
                        current_id,
                        description=video.episode_title,
                        completed=100.0,
                    )
                progress.update(overall_id, advance=1)


class ProgressRenderer:
    """Daemon producer/consumer bridge for Rich progress updates."""

    def __init__(self, videos: list[Video]) -> None:
        self._events: queue.Queue[ProgressEvent | None] = queue.Queue()
        self._thread = threading.Thread(
            target=_render,
            args=(videos, self._events),
            name="iyf-progress",
            daemon=True,
        )
        self._thread.start()

    def callback(self, episode_index: int) -> Callable[[float], None]:
        """Return a tiny yt-dlp hook that only enqueues progress."""

        def enqueue(percent: float) -> None:
            self._events.put(ProgressEvent(episode_index, percent))

        return enqueue

    def finish(self, episode_index: int) -> None:
        """Mark one episode complete."""
        self._events.put(ProgressEvent(episode_index, 100.0, finished=True))

    def close(self) -> None:
        """Stop the renderer after all queued events have been consumed."""
        self._events.put(None)
        self._thread.join()
