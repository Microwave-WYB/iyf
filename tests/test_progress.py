"""Unit tests for byte-based download progress."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from iyf import ProgressRenderer, Video, engine, progress


def media_playlist(
    segments: list[tuple[str, float]], extinf: bool = True, endlist: bool = True
) -> str:
    """Build a VOD media playlist for the given ``(url, duration)`` pairs."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:10"]
    for url, duration in segments:
        if extinf:
            lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(url)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


class FakeResponse:
    """Minimal ``httpx`` response stand-in for HEAD probes."""

    def __init__(
        self, content_length: int | None = None, status_code: int = 200
    ) -> None:
        self.status_code = status_code
        self.headers = (
            {} if content_length is None else {"content-length": str(content_length)}
        )

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise engine.httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=engine.httpx.Request("HEAD", "https://cdn.example/segment.ts"),
                response=engine.httpx.Response(self.status_code),
            )


def fake_head(sizes: dict[str, int]):
    """Return a stand-in for ``httpx.head`` that answers with ``Content-Length``."""

    def head(url: str, **_kwargs: object) -> object:
        if url not in sizes:
            raise engine.httpx.HTTPError(f"no size for {url}")
        return FakeResponse(sizes[url])

    return head


class PlaylistParsingTest(unittest.TestCase):
    def test_segments_resolve_relative_urls(self) -> None:
        text = media_playlist([("seg0.ts", 4.0), ("https://cdn.example/seg1.ts", 6.0)])
        urls, durations = engine._playlist_segments(
            text, "https://cdn.example/hls/index.m3u8"
        )
        self.assertEqual(
            urls, ["https://cdn.example/hls/seg0.ts", "https://cdn.example/seg1.ts"]
        )
        self.assertEqual(durations, [4.0, 6.0])

    def test_segments_without_extinf_report_zero_duration(self) -> None:
        text = media_playlist([("seg0.ts", 0.0), ("seg1.ts", 0.0)], extinf=False)
        urls, durations = engine._playlist_segments(
            text, "https://cdn.example/index.m3u8"
        )
        self.assertEqual(len(urls), 2)
        self.assertEqual(durations, [0.0, 0.0])

    def test_master_playlist_prefers_highest_bandwidth(self) -> None:
        text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\nlow.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=3684000,RESOLUTION=1920x1080\nhigh.m3u8\n"
        )
        self.assertEqual(
            engine._best_variant("https://cdn.example/master.m3u8", text),
            "https://cdn.example/high.m3u8",
        )

    def test_master_playlist_without_stream_inf_has_no_variant(self) -> None:
        self.assertIsNone(
            engine._best_variant("https://cdn.example/master.m3u8", "#EXTM3U\n")
        )


class ProbeTotalBytesTest(unittest.TestCase):
    def test_full_sweep_sums_every_segment(self) -> None:
        text = media_playlist([("a.ts", 5.0), ("b.ts", 5.0), ("c.ts", 10.0)])
        sizes = {
            "https://cdn.example/a.ts": 1_000_000,
            "https://cdn.example/b.ts": 2_000_000,
            "https://cdn.example/c.ts": 7_000_000,
        }
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            self.assertEqual(
                engine.probe_total_bytes("https://cdn.example/index.m3u8"), 10_000_000
            )

    def test_dead_playlist_is_rejected(self) -> None:
        # 49684 line 2 lists 100 segment URLs but declares no media time.
        text = media_playlist([(f"seg{i}.ts", 0.0) for i in range(100)], extinf=False)
        with mock.patch.object(engine, "_http_get", return_value=text):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_live_playlist_is_rejected(self) -> None:
        # Without ENDLIST the segment list keeps growing, so it is no total.
        text = media_playlist([("a.ts", 4.0), ("b.ts", 4.0)], endlist=False)
        with mock.patch.object(engine, "_http_get", return_value=text):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_unanswered_segments_respect_the_deadline(self) -> None:
        # A slow CDN must not stall the download the probe is measuring.
        text = media_playlist([(f"seg{i}.ts", 10.0) for i in range(6)])
        sizes = {f"https://cdn.example/seg{i}.ts": 1_000_000 for i in range(6)}

        def slow_head(url: str, **_kwargs: object) -> object:
            if url.endswith(("seg0.ts", "seg1.ts")):
                return mock.Mock(headers={"content-length": str(sizes[url])})
            time.sleep(0.6)
            return mock.Mock(headers={"content-length": str(sizes[url])})

        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=slow_head),
        ):
            started = time.monotonic()
            total = engine.probe_total_bytes(
                "https://cdn.example/index.m3u8", deadline=0.2
            )
            elapsed = time.monotonic() - started
        # 2 MB measured over 20 s of media, scaled to the full 60 s.
        self.assertEqual(total, 6_000_000)
        self.assertLess(elapsed, 1.0)

    def test_master_playlist_is_followed_to_its_variant(self) -> None:
        master = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=3684000,RESOLUTION=1920x1080\n"
            "1080/index.m3u8\n"
        )
        media = media_playlist([("a.ts", 4.0), ("b.ts", 4.0)])
        sizes = {
            "https://cdn.example/hls/1080/a.ts": 900_000,
            "https://cdn.example/hls/1080/b.ts": 1_100_000,
        }
        with (
            mock.patch.object(engine, "_http_get", side_effect=[master, media]) as get,
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            total = engine.probe_total_bytes("https://cdn.example/hls/master.m3u8")
        self.assertEqual(total, 2_000_000)
        self.assertEqual(
            get.call_args_list[1].args[0], "https://cdn.example/hls/1080/index.m3u8"
        )

    def test_missing_content_length_returns_none(self) -> None:
        text = media_playlist([("a.ts", 4.0), ("b.ts", 4.0)])
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head({})),
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_unreachable_playlist_returns_none(self) -> None:
        with mock.patch.object(
            engine, "_http_get", side_effect=engine.IyfError("boom")
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_partial_failures_are_scaled_by_media_time(self) -> None:
        text = media_playlist([(f"seg{i}.ts", 10.0) for i in range(4)])
        sizes = {  # the fourth segment never answers
            "https://cdn.example/seg0.ts": 1_000_000,
            "https://cdn.example/seg1.ts": 1_000_000,
            "https://cdn.example/seg2.ts": 1_000_000,
        }
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            # 3 MB measured over 30 s of media, scaled to the full 40 s.
            total = engine.probe_total_bytes("https://cdn.example/index.m3u8")
        self.assertEqual(total, 4_000_000)

    def test_too_few_measurements_return_none(self) -> None:
        text = media_playlist([(f"seg{i}.ts", 10.0) for i in range(3)])
        sizes = {"https://cdn.example/seg0.ts": 1_000_000}
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_error_statuses_are_not_counted(self) -> None:
        text = media_playlist([("a.ts", 5.0), ("b.ts", 5.0), ("c.ts", 5.0)])
        sizes = {
            "https://cdn.example/a.ts": 1_000_000,
            "https://cdn.example/b.ts": 1_000_000,
            "https://cdn.example/c.ts": 3_000_000,
        }

        def head(url: str, **_kwargs: object) -> object:
            # A 404 still carries a Content-Length; it must not count as data.
            return FakeResponse(sizes[url], status_code=404 if "b.ts" in url else 200)

        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=head),
        ):
            total = engine.probe_total_bytes("https://cdn.example/index.m3u8")
        # 4 MB measured over 10 s of media, scaled to the full 15 s.
        self.assertEqual(total, 6_000_000)

    def test_single_segment_playlist_is_usable(self) -> None:
        text = media_playlist([("a.ts", 5.0)])
        sizes = {"https://cdn.example/a.ts": 1_234}
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            self.assertEqual(
                engine.probe_total_bytes("https://cdn.example/index.m3u8"), 1_234
            )

    def test_malformed_duration_returns_no_denominator(self) -> None:
        # Keeping the URL but dropping its duration would silently drop that
        # segment's bytes from the total, so nothing is reported instead.
        text = (
            "#EXTM3U\n"
            "#EXTINF:1e309,\nbad.ts\n"
            "#EXTINF:10.0,\ngood1.ts\n"
            "#EXTINF:10.0,\ngood2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        sizes = {
            "https://cdn.example/bad.ts": 9_000,
            "https://cdn.example/good1.ts": 1_000,
            "https://cdn.example/good2.ts": 1_000,
        }
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_zero_duration_segment_returns_no_denominator(self) -> None:
        text = media_playlist([("a.ts", 0.0), ("b.ts", 10.0)])
        sizes = {
            "https://cdn.example/a.ts": 1_000,
            "https://cdn.example/b.ts": 1_000,
        }
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head", side_effect=fake_head(sizes)),
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )

    def test_playlist_reads_use_the_remaining_deadline(self) -> None:
        seen: list[float] = []

        def fetch(url: str, timeout: float = 30.0) -> str:
            seen.append(timeout)
            raise engine.IyfError("boom")

        with mock.patch.object(engine, "_http_get", side_effect=fetch):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8", deadline=0.5)
            )
        self.assertTrue(seen)
        self.assertLessEqual(seen[0], 0.5)

    def test_expired_deadline_skips_the_playlist_read(self) -> None:
        with mock.patch.object(engine, "_http_get") as fetch:
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8", deadline=0.0)
            )
        fetch.assert_not_called()

    def test_absurd_playlists_are_refused_without_network(self) -> None:
        # Parsing is the one phase the deadline cannot interrupt, so the size
        # bound has to reject it before any request is made.
        text = media_playlist([(f"seg{i}.ts", 5.0) for i in range(120_000)])
        self.assertGreater(len(text), engine._MAX_PROBE_PLAYLIST_BYTES)
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine.httpx, "head") as head,
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/index.m3u8")
            )
        head.assert_not_called()

    def test_absurd_master_playlists_are_refused_before_parsing(self) -> None:
        # The size bound lives in fetch(), so an oversized master is refused
        # before _best_variant() walks it.
        variants = "".join(
            f"#EXT-X-STREAM-INF:BANDWIDTH={index}\nv{index}.m3u8\n"
            for index in range(150_000)
        )
        text = f"#EXTM3U\n{variants}#EXT-X-ENDLIST\n"
        self.assertGreater(len(text), engine._MAX_PROBE_PLAYLIST_BYTES)
        with (
            mock.patch.object(engine, "_http_get", return_value=text),
            mock.patch.object(engine, "_best_variant") as best,
            mock.patch.object(engine.httpx, "head") as head,
        ):
            self.assertIsNone(
                engine.probe_total_bytes("https://cdn.example/master.m3u8")
            )
        best.assert_not_called()
        head.assert_not_called()


class DownloadOptionsTest(unittest.TestCase):
    """
    The options handed to yt-dlp must silence its own progress bar, and the
    size probe must not run when nothing reports progress.
    """

    URL = "https://cdn.example/index.m3u8"
    OUT = "/tmp/iyf-options-test/out.mp4"

    def _run(
        self,
        *,
        progress_cb: object = None,
        show_progress: bool = False,
        verbose: bool = False,
    ) -> dict[str, object]:
        captured: dict[str, object] = {}

        class FakeYDL:
            def __init__(self, options: dict[str, object]) -> None:
                captured.update(options)

            def __enter__(self) -> "FakeYDL":
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def download(self, _urls: list[str]) -> int:
                return 0

        with mock.patch.object(engine.yt_dlp, "YoutubeDL", FakeYDL):
            engine.download(
                self.URL,
                self.OUT,
                verbose=verbose,
                show_progress=show_progress,
                progress_cb=progress_cb,  # type: ignore[arg-type]
            )
        return captured

    def test_noprogress_silences_yt_dlp_unless_verbose(self) -> None:
        with mock.patch.object(engine, "probe_total_bytes", return_value=1_000):
            self.assertIs(
                self._run(progress_cb=lambda _sample: None)["noprogress"], True
            )
            self.assertIs(
                self._run(progress_cb=lambda _sample: None, verbose=True)["noprogress"],
                False,
            )

    def test_probe_is_skipped_when_nothing_reports_progress(self) -> None:
        with mock.patch.object(engine, "probe_total_bytes") as probe:
            self._run()
        probe.assert_not_called()

    def test_probe_runs_when_a_callback_reports_progress(self) -> None:
        with mock.patch.object(
            engine, "probe_total_bytes", return_value=4_242
        ) as probe:
            self._run(progress_cb=lambda _sample: None)
        probe.assert_called_once_with(self.URL)


class PartBytesTest(unittest.TestCase):
    def test_glob_metacharacters_in_the_output_path_are_literal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out_path = str(Path(directory) / "episode[0].mp4")
            Path(directory, "episode0.mp4.part-Frag1").write_bytes(b"x" * 7)
            self.assertEqual(engine._part_bytes(out_path), 0)

            Path(f"{out_path}.part").write_bytes(b"x" * 3)
            Path(f"{out_path}.part-Frag2").write_bytes(b"x" * 4)
            self.assertEqual(engine._part_bytes(out_path), 7)


class WatchPartFileTest(unittest.TestCase):
    def test_reports_a_growing_part_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out_path = str(Path(directory) / "episode.mp4")
            samples: list[engine.ProgressSample] = []
            hooked = threading.Event()
            stop = threading.Event()
            thread = threading.Thread(
                target=engine._watch_part_file,
                args=(out_path, 5_000, samples.append, hooked, stop),
                kwargs={"grace": 0.05, "interval": 0.02},
                daemon=True,
            )
            thread.start()
            Path(f"{out_path}.part").write_bytes(b"x" * 1_000)
            time.sleep(0.15)
            Path(f"{out_path}.part-Frag3").write_bytes(b"y" * 1_500)
            time.sleep(0.15)
            stop.set()
            thread.join(timeout=2.0)

        self.assertTrue(samples)
        self.assertEqual({sample.total for sample in samples}, {5_000})
        self.assertGreaterEqual(samples[-1].downloaded, 2_500)
        sizes = [sample.downloaded for sample in samples]
        self.assertEqual(sizes, sorted(sizes))

    def test_no_polling_when_yt_dlp_reports_progress(self) -> None:
        samples: list[engine.ProgressSample] = []
        hooked = threading.Event()
        hooked.set()
        engine._watch_part_file(
            "/nonexistent/episode.mp4",
            None,
            samples.append,
            hooked,
            threading.Event(),
            grace=5.0,
            interval=0.01,
        )
        self.assertEqual(samples, [])


class RendererTest(unittest.TestCase):
    @staticmethod
    def video(episode: str = "1", title: str = "第1集") -> Video:
        return Video(
            link="https://www.iyf.lv/iyftv/1/",
            show_id="1",
            line=1,
            show_name="示例剧",
            season=1,
            episode=episode,
            episode_title=title,
            media_class="视频",
            stream_url="https://cdn.example/index.m3u8",
        )

    @staticmethod
    def _run(
        videos: list[Video],
        events: list[tuple[int, engine.ProgressSample | None]],
    ) -> dict[str, object]:
        """Render ``events`` and return the Rich tasks, keyed by description."""
        tasks: dict[str, object] = {}
        original = progress.Progress

        class Spy(original):  # type: ignore[misc, valid-type]
            def add_task(self, description: str, **kwargs: object) -> object:
                task_id = super().add_task(description, **kwargs)  # type: ignore[arg-type]
                tasks[description] = self.tasks[task_id]  # type: ignore[index]
                return task_id

        with mock.patch.object(progress, "Progress", Spy):
            renderer = ProgressRenderer(videos)
            for index, sample in events:
                if sample is None:
                    renderer.finish(index)
                else:
                    renderer.callback(index)(sample)
            renderer.close()
        return tasks

    def test_renderer_accepts_total_only_and_completion_events(self) -> None:
        # No finish event: a completed episode resets its task (see below).
        tasks = self._run(
            [self.video()],
            [
                (0, engine.ProgressSample(downloaded=1_000, total=None)),
                (0, engine.ProgressSample(total=3_000)),
                (
                    0,
                    engine.ProgressSample(downloaded=3_000, total=3_000, finished=True),
                ),
            ],
        )
        self.assertEqual(tasks["第1集"].total, 3_000)
        self.assertEqual(tasks["第1集"].completed, 3_000)

    def test_episode_task_is_marked_as_a_byte_task(self) -> None:
        # Regression guard: Rich's add_task takes ``**fields``, so passing
        # ``fields={...}`` leaves task.fields["bytes"] unset and the byte and
        # speed columns render as empty strings.
        tasks = self._run(
            [self.video()],
            [(0, engine.ProgressSample(downloaded=1, total=2))],
        )
        self.assertEqual(tasks["第1集"].fields, {"bytes": True})
        self.assertNotIn("bytes", tasks["总进度"].fields)

    def test_renderer_without_total_never_crashes(self) -> None:
        tasks = self._run([self.video()], [(0, engine.ProgressSample(downloaded=512))])
        self.assertIsNone(tasks["第1集"].total)
        self.assertEqual(tasks["第1集"].completed, 512)

    def test_finishing_an_episode_clears_its_task(self) -> None:
        # The task is reused for the next episode, so a finished episode must
        # not leave its denominator or byte count behind.
        tasks = self._run(
            [self.video()],
            [
                (0, engine.ProgressSample(downloaded=100, total=100)),
                (0, None),
            ],
        )
        self.assertIsNone(tasks["第1集"].total)
        self.assertEqual(tasks["第1集"].completed, 0)
        self.assertEqual(tasks["总进度"].completed, 1)

    def test_second_episode_does_not_inherit_the_previous_total(self) -> None:
        # The episode bar is a single reused task, so the second episode is
        # the same task object with a new description.
        tasks = self._run(
            [self.video("1", "第1集"), self.video("2", "第2集")],
            [
                (0, engine.ProgressSample(downloaded=100, total=100)),
                (0, None),
                (1, engine.ProgressSample(downloaded=10, total=None)),
            ],
        )
        episode = tasks["第1集"]
        self.assertEqual(episode.description, "第2集")
        self.assertIsNone(episode.total)
        self.assertEqual(episode.completed, 10)
        self.assertEqual(tasks["总进度"].completed, 1)

    def test_bar_never_moves_backwards(self) -> None:
        # yt-dlp hooks and the .part watcher both report, so samples can
        # arrive out of order.
        tasks = self._run(
            [self.video()],
            [
                (0, engine.ProgressSample(downloaded=100, total=None)),
                (0, engine.ProgressSample(downloaded=50, total=None)),
            ],
        )
        self.assertEqual(tasks["第1集"].completed, 100)

    def test_bar_is_clamped_to_a_late_total(self) -> None:
        tasks = self._run(
            [self.video()],
            [
                (0, engine.ProgressSample(downloaded=100, total=None)),
                (0, engine.ProgressSample(downloaded=100, total=80)),
            ],
        )
        self.assertEqual(tasks["第1集"].completed, 80)

    def test_overall_bar_advances_once_per_episode(self) -> None:
        tasks = self._run(
            [self.video("1", "第1集"), self.video("2", "第2集")],
            [
                (0, engine.ProgressSample(downloaded=100, total=100, finished=True)),
                (0, None),
                (1, engine.ProgressSample(downloaded=100, total=100, finished=True)),
                (1, None),
            ],
        )
        self.assertEqual(tasks["总进度"].completed, 2)


if __name__ == "__main__":
    unittest.main()
