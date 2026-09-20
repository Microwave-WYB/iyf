"""Unit tests for the media-library layout, .strm writing and refresh."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import iyf
from iyf import (
    SIDECAR_NAME,
    Video,
    engine,
    library_path,
    refresh_streams,
    write_streams,
)


def series_video(
    episode: str = "2", season: int = 1, name: str = "权力的游戏"
) -> Video:
    return Video(
        link="https://www.iyf.lv/iyftv/95676/",
        show_id="95676",
        line=2,
        show_name=name,
        season=season,
        episode=episode,
        episode_title=f"第{episode}集",
        media_class="欧美",
        stream_url="https://cdn.example/old/index.m3u8",
        is_series=True,
    )


def single_video() -> Video:
    return Video(
        link="https://www.iyf.lv/iyftv/101575/",
        show_id="101575",
        line=1,
        show_name="权力的游戏：最后的守夜人",
        season=1,
        episode="1",
        episode_title="正片",
        media_class="纪录",
        stream_url="https://cdn.example/movie/index.m3u8",
        is_series=False,
    )


class LayoutTest(unittest.TestCase):
    def test_series_paths_use_a_season_folder(self) -> None:
        video = series_video(episode="3", season=7, name="生活大爆炸")
        self.assertEqual(
            library_path("iyf_downloads", video, video.filename),
            Path("iyf_downloads/生活大爆炸/Season 07/生活大爆炸 S07E03.mp4"),
        )
        self.assertEqual(
            library_path("/mnt/media", video, video.stream_filename),
            Path("/mnt/media/生活大爆炸/Season 07/生活大爆炸 S07E03.strm"),
        )

    def test_single_videos_are_not_named_like_episodes(self) -> None:
        video = single_video()
        self.assertEqual(video.filename, "权力的游戏：最后的守夜人.mp4")
        self.assertEqual(video.stream_filename, "权力的游戏：最后的守夜人.strm")
        self.assertEqual(
            library_path("/mnt/media", video, video.stream_filename),
            Path("/mnt/media/权力的游戏：最后的守夜人/权力的游戏：最后的守夜人.strm"),
        )

    def test_output_root_replaces_the_default_one(self) -> None:
        video = series_video()
        self.assertEqual(
            library_path("/mnt/storage/media", video, video.filename),
            Path("/mnt/storage/media/权力的游戏/Season 01/权力的游戏 S01E02.mp4"),
        )


class WriteStreamsTest(unittest.TestCase):
    def test_writes_single_line_url_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(iyf, "resolve_all", return_value=[series_video()]):
                paths = write_streams("权力的游戏 第一季", directory, "1-2")
            self.assertEqual(
                paths,
                [Path(directory) / "权力的游戏/Season 01/权力的游戏 S01E02.strm"],
            )
            self.assertEqual(
                paths[0].read_text(encoding="utf-8"),
                "https://cdn.example/old/index.m3u8\n",
            )
            sidecar = json.loads(
                (Path(directory) / "权力的游戏" / "Season 01" / SIDECAR_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                sidecar, {"show_id": "95676", "line": 2, "title": "权力的游戏"}
            )

    def test_writing_another_source_into_the_same_season_is_refused(self) -> None:
        # Two iyf entries can carry the same title and season (49684 and 95676
        # are both 权力的游戏 第一季), so the second must not silently take over
        # the folder the first one filled.
        other = Video(
            link="https://www.iyf.lv/iyftv/49684/",
            show_id="49684",
            line=1,
            show_name="权力的游戏",
            season=1,
            episode="1",
            episode_title="第1集",
            media_class="欧美",
            stream_url="https://cdn.example/other/index.m3u8",
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                iyf, "resolve_all", return_value=[series_video("1")]
            ):
                write_streams("权力的游戏 第一季", directory, "1")
            season = Path(directory) / "权力的游戏" / "Season 01"
            with (
                mock.patch.object(iyf, "resolve_all", return_value=[other]),
                self.assertRaises(engine.IyfError),
            ):
                write_streams("权力的游戏第一季", directory, "1")

            self.assertEqual(
                (season / "权力的游戏 S01E01.strm").read_text(encoding="utf-8"),
                "https://cdn.example/old/index.m3u8\n",
            )
            self.assertEqual(
                json.loads((season / SIDECAR_NAME).read_text(encoding="utf-8"))[
                    "show_id"
                ],
                "95676",
            )


class _StubEngine:
    """Stand in for the engine calls refresh_streams makes."""

    def __init__(self, url: str, valid: bool = True) -> None:
        self.url = url
        self.valid = valid

    def __enter__(self) -> "_StubEngine":
        self._patches = [
            mock.patch.object(engine, "get_show", side_effect=self._get_show),
            mock.patch.object(engine, "get_play_info", side_effect=self._play_info),
            mock.patch.object(
                engine,
                "inspect_playlist_url",
                return_value=engine.PlaylistInspection(valid=self.valid, duration=60.0),
            ),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        for patch in self._patches:
            patch.stop()

    @staticmethod
    def _get_show(show_id: str) -> engine.Series:
        episodes = [engine.Episode("1", "第1集"), engine.Episode("2", "第2集")]
        # Real entries expose several lines; sidebar fixtures use line 1 and 2.
        return engine.Series(
            show_id,
            "权力的游戏 第一季",
            [engine.Line(1, episodes), engine.Line(2, episodes)],
        )

    def _play_info(self, show_id: str, line: int, episode: str) -> engine.PlayInfo:
        return engine.PlayInfo(self.url, "欧美")


class RefreshStreamsTest(unittest.TestCase):
    def _library(self, directory: str) -> tuple[Path, Path]:
        series = Path(directory) / "权力的游戏"
        season = series / "Season 01"
        season.mkdir(parents=True, exist_ok=True)
        episode = season / "权力的游戏 S01E02.strm"
        episode.write_text("https://cdn.example/old/index.m3u8\n", encoding="utf-8")
        (season / SIDECAR_NAME).write_text(
            json.dumps({"show_id": "95676", "line": 2, "title": "权力的游戏"}),
            encoding="utf-8",
        )
        return series, episode

    def test_unchanged_url_is_reported_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, episode = self._library(directory)
            with _StubEngine("https://cdn.example/old/index.m3u8"):
                statuses = refresh_streams(directory)
        self.assertEqual([item.status for item in statuses], ["ok"])
        self.assertEqual(statuses[0].path.name, "权力的游戏 S01E02.strm")

    def test_changed_url_is_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, episode = self._library(directory)
            with _StubEngine("https://cdn.example/new/index.m3u8"):
                statuses = refresh_streams(directory)
            self.assertEqual([item.status for item in statuses], ["updated"])
            self.assertEqual(
                episode.read_text(encoding="utf-8"),
                "https://cdn.example/new/index.m3u8\n",
            )

    def test_check_only_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, episode = self._library(directory)
            with _StubEngine("https://cdn.example/new/index.m3u8"):
                statuses = refresh_streams(directory, check_only=True)
            self.assertEqual([item.status for item in statuses], ["updated"])
            self.assertEqual(
                episode.read_text(encoding="utf-8"),
                "https://cdn.example/old/index.m3u8\n",
            )

    def test_unplayable_playlist_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, episode = self._library(directory)
            with _StubEngine("https://cdn.example/new/index.m3u8", valid=False):
                statuses = refresh_streams(directory)
            self.assertEqual([item.status for item in statuses], ["broken"])
            self.assertIn("not playable", statuses[0].detail)
            self.assertEqual(
                episode.read_text(encoding="utf-8"),
                "https://cdn.example/old/index.m3u8\n",
            )

    def test_directories_without_a_sidecar_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stray = Path(directory) / "somewhere" / "x.strm"
            stray.parent.mkdir(parents=True)
            stray.write_text("https://cdn.example/x.m3u8\n", encoding="utf-8")
            self.assertEqual(refresh_streams(directory), [])

    def test_single_video_file_uses_the_only_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            series = Path(directory) / "权力的游戏：最后的守夜人"
            series.mkdir(parents=True)
            movie = series / "权力的游戏：最后的守夜人.strm"
            movie.write_text("https://cdn.example/old/index.m3u8\n", encoding="utf-8")
            (series / SIDECAR_NAME).write_text(
                json.dumps(
                    {
                        "show_id": "101575",
                        "line": 1,
                        "title": "权力的游戏：最后的守夜人",
                    }
                ),
                encoding="utf-8",
            )
            with _StubEngine("https://cdn.example/new/index.m3u8"):
                statuses = refresh_streams(directory)
            self.assertEqual([item.status for item in statuses], ["updated"])
            self.assertEqual(
                movie.read_text(encoding="utf-8"),
                "https://cdn.example/new/index.m3u8\n",
            )

    def test_each_season_refreshes_against_its_own_source(self) -> None:
        # Every season of one title shares a title directory, so the sidecar
        # has to live in the season folder: one sidecar per title would let the
        # last written season decide what all the others refresh from.
        def show(show_id: str) -> engine.Series:
            episodes = [engine.Episode("1", "第1集"), engine.Episode("2", "第2集")]
            return engine.Series(show_id, "权力的游戏", [engine.Line(2, episodes)])

        def play_info(show_id: str, line: int, episode: str) -> engine.PlayInfo:
            return engine.PlayInfo(
                f"https://cdn.example/{show_id}/S{line}E{episode}.m3u8", None
            )

        with tempfile.TemporaryDirectory() as directory:
            for season, show_id in (("01", "95676"), ("07", "101299")):
                season_dir = Path(directory) / "权力的游戏" / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"权力的游戏 S{season}E01.strm").write_text(
                    "https://cdn.example/old.m3u8\n", encoding="utf-8"
                )
                (season_dir / SIDECAR_NAME).write_text(
                    json.dumps({"show_id": show_id, "line": 2, "title": "权力的游戏"}),
                    encoding="utf-8",
                )
            with (
                mock.patch.object(engine, "get_show", side_effect=show),
                mock.patch.object(engine, "get_play_info", side_effect=play_info),
                mock.patch.object(
                    engine,
                    "inspect_playlist_url",
                    return_value=engine.PlaylistInspection(valid=True, duration=60.0),
                ),
            ):
                statuses = refresh_streams(directory)
            self.assertEqual([item.status for item in statuses], ["updated", "updated"])
            urls = sorted(item.url for item in statuses)
            self.assertEqual(
                urls,
                [
                    "https://cdn.example/101299/S2E1.m3u8",
                    "https://cdn.example/95676/S2E1.m3u8",
                ],
            )
            for season, show_id in (("01", "95676"), ("07", "101299")):
                written = (
                    Path(directory)
                    / "权力的游戏"
                    / f"Season {season}"
                    / f"权力的游戏 S{season}E01.strm"
                ).read_text(encoding="utf-8")
                self.assertEqual(written, f"https://cdn.example/{show_id}/S2E1.m3u8\n")

    def test_other_tools_strm_files_are_left_alone(self) -> None:
        # A sidecar claims this title's files, not everything that happens to
        # sit beside them.
        with tempfile.TemporaryDirectory() as directory:
            _, episode = self._library(directory)
            stray = episode.parent / "other-tool.strm"
            stray.write_text("https://other.example/x.m3u8\n", encoding="utf-8")
            with _StubEngine("https://cdn.example/new/index.m3u8"):
                statuses = refresh_streams(directory)
            self.assertEqual(
                [item.path.name for item in statuses], ["权力的游戏 S01E02.strm"]
            )
            self.assertEqual(
                stray.read_text(encoding="utf-8"), "https://other.example/x.m3u8\n"
            )

    def test_lookalike_files_are_not_treated_as_this_title(self) -> None:
        # "<title> Sideload.strm" is not "<title> SxxExx.strm".
        with tempfile.TemporaryDirectory() as directory:
            _, episode = self._library(directory)
            lookalike = episode.parent / "权力的游戏 Sideload.strm"
            lookalike.write_text("https://other.example/x.m3u8\n", encoding="utf-8")
            with _StubEngine("https://cdn.example/new/index.m3u8"):
                statuses = refresh_streams(directory)
            self.assertEqual(
                [item.path.name for item in statuses], ["权力的游戏 S01E02.strm"]
            )
            self.assertEqual(
                lookalike.read_text(encoding="utf-8"), "https://other.example/x.m3u8\n"
            )

    def test_sidecar_without_a_title_is_ignored(self) -> None:
        # Without a title the directory cannot be filtered safely, so it is not
        # treated as iyf's to manage.
        with tempfile.TemporaryDirectory() as directory:
            series, _ = self._library(directory)
            (series / "Season 01" / SIDECAR_NAME).write_text(
                json.dumps({"show_id": "95676", "line": 2}), encoding="utf-8"
            )
            self.assertEqual(refresh_streams(directory), [])


class SeriesDetectionTest(unittest.TestCase):
    @staticmethod
    def _resolve(series: engine.Series) -> list[iyf.Video]:
        with (
            mock.patch.object(
                iyf, "_source", return_value=engine.Source(series.show_id, 2)
            ),
            mock.patch.object(engine, "get_show", return_value=series),
            mock.patch.object(
                engine, "get_play_info", return_value=engine.PlayInfo("u", None)
            ),
        ):
            return iyf.resolve_all(series.show_id, "1")

    def test_two_lines_exposing_one_episode_is_not_a_series(self) -> None:
        episodes = [engine.Episode("1", "正片")]
        series = engine.Series(
            "101575", "某纪录片", [engine.Line(1, episodes), engine.Line(2, episodes)]
        )
        video = self._resolve(series)[0]
        self.assertFalse(video.is_series)
        self.assertEqual(video.filename, "某纪录片.mp4")

    def test_multiple_episodes_is_a_series(self) -> None:
        episodes = [engine.Episode("1", "第1集"), engine.Episode("2", "第2集")]
        series = engine.Series("95676", "权力的游戏 第一季", [engine.Line(2, episodes)])
        video = self._resolve(series)[0]
        self.assertTrue(video.is_series)
        self.assertEqual(video.filename, "权力的游戏 S01E01.mp4")


if __name__ == "__main__":
    unittest.main()
