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


def fake_head(sizes: dict[str, int]):
    """Return a stand-in for ``httpx.head`` that answers with ``Content-Length``."""

    def head(url: str, **_kwargs: object) -> object:
        if url not in sizes:
            raise engine.httpx.HTTPError(f"no size for {url}")
        return mock.Mock(headers={"content-length": str(sizes[url])})

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
    def video() -> Video:
        return Video(
            link="https://www.iyf.lv/iyftv/1/",
            show_id="1",
            line=1,
            show_name="示例剧",
            season=1,
            episode="1",
            episode_title="第1集",
            media_class="视频",
            stream_url="https://cdn.example/index.m3u8",
        )

    def test_renderer_accepts_total_only_and_completion_events(self) -> None:
        renderer = ProgressRenderer([self.video()])
        callback = renderer.callback(0)
        callback(engine.ProgressSample(downloaded=1_000, total=None))
        callback(engine.ProgressSample(total=3_000))  # probe result, no bytes yet
        callback(engine.ProgressSample(downloaded=3_000, total=3_000, finished=True))
        renderer.finish(0)
        renderer.close()

    def test_episode_task_is_marked_as_a_byte_task(self) -> None:
        # Regression guard: Rich's add_task takes ``**fields``, so passing
        # ``fields={...}`` leaves task.fields["bytes"] unset and the byte and
        # speed columns render as empty strings.
        seen: dict[str, dict[str, object]] = {}
        original = progress.Progress

        class Spy(original):  # type: ignore[misc, valid-type]
            def add_task(self, description: str, **kwargs: object) -> object:
                task_id = super().add_task(description, **kwargs)  # type: ignore[arg-type]
                seen[description] = self.tasks[task_id].fields  # type: ignore[index]
                return task_id

        with mock.patch.object(progress, "Progress", Spy):
            renderer = ProgressRenderer([self.video()])
            renderer.callback(0)(engine.ProgressSample(downloaded=1, total=2))
            renderer.finish(0)
            renderer.close()
        self.assertEqual(seen["第1集"], {"bytes": True})
        self.assertNotIn("bytes", seen["总进度"])

    def test_renderer_without_total_never_crashes(self) -> None:
        renderer = ProgressRenderer([self.video()])
        renderer.callback(0)(engine.ProgressSample(downloaded=512))
        renderer.finish(0)
        renderer.close()


if __name__ == "__main__":
    unittest.main()
