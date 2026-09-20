"""Queued Rich progress rendering for multi-episode downloads."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    Task,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from .engine import ProgressSample

if TYPE_CHECKING:
    from . import Video


class _ByteColumn(DownloadColumn):
    """``DownloadColumn`` that stays empty for tasks that do not count bytes."""

    def render(self, task: Task) -> Text:
        if not task.fields.get("bytes"):
            return Text("")
        return super().render(task)


class _ByteSpeedColumn(TransferSpeedColumn):
    """``TransferSpeedColumn`` that stays empty for tasks that do not count bytes."""

    def render(self, task: Task) -> Text:
        if not task.fields.get("bytes"):
            return Text("")
        return super().render(task)


@dataclass
class ProgressEvent:
    """One live sample, or the completion of an episode when ``sample`` is None."""

    episode_index: int
    sample: ProgressSample | None = None


def _render(videos: list[Video], events: queue.Queue[ProgressEvent | None]) -> None:
    # Bytes, not percentages: DownloadColumn needs a byte total and
    # TransferSpeedColumn derives the rate from update cadence, so this only
    # has to forward ``downloaded`` and ``total``.
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        _ByteColumn(),
        _ByteSpeedColumn(),
        TimeRemainingColumn(),
    )
    with progress:
        current_id = progress.add_task(
            videos[0].episode_title,
            total=None,
            visible=False,
            # ``add_task`` takes fields as keywords: ``bytes=True`` becomes
            # ``task.fields["bytes"]``, which the byte columns look for.
            bytes=True,
        )
        overall_id = progress.add_task("总进度", total=len(videos))
        current_visible = False
        warning_shown = False
        while True:
            event = events.get()
            if event is None:
                return
            video = videos[event.episode_index]
            if event.sample is None:
                if not current_visible and not warning_shown:
                    progress.console.print(
                        "本集无法获取实时进度，仅显示总进度。", style="yellow"
                    )
                    warning_shown = True
                if current_visible:
                    progress.update(current_id, visible=False)
                    current_visible = False
                progress.update(overall_id, advance=1)
                continue
            sample = event.sample
            if sample.total is not None:
                progress.update(current_id, total=float(sample.total))
            if sample.downloaded is None:
                continue
            if not current_visible:
                progress.update(
                    current_id, description=video.episode_title, visible=True
                )
                current_visible = True
            progress.update(current_id, completed=float(sample.downloaded))


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

    def callback(self, episode_index: int) -> Callable[[ProgressSample], None]:
        """Return a tiny yt-dlp hook that only enqueues progress."""

        def enqueue(sample: ProgressSample) -> None:
            self._events.put(ProgressEvent(episode_index, sample))

        return enqueue

    def finish(self, episode_index: int) -> None:
        """Mark one episode complete."""
        self._events.put(ProgressEvent(episode_index))

    def close(self) -> None:
        """Stop the renderer after all queued events have been consumed."""
        self._events.put(None)
        self._thread.join()
