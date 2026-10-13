"""合成模块的单元测试（阶段七）。

只验证命令构造与规格校验，不真的调用 FFmpeg，所以跑得飞快。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import compose  # noqa: E402


class CommandBuildTest(unittest.TestCase):
    def test_normalize_command_has_fades_and_copies_video(self) -> None:
        cmd = compose.build_normalize_command(
            Path("ffmpeg.exe"), Path("in.mp4"), Path("out.mp4"),
            fade_seconds=0.08, duration=15.08,
        )
        text = " ".join(cmd)
        self.assertIn("-c:v copy", text, "视频流不应重编码")
        self.assertIn("afade=t=in:st=0:d=0.08", text)
        self.assertIn("afade=t=out:st=15.000:d=0.08", text)
        self.assertIn("-c:a aac", text)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_normalize_command_without_fade(self) -> None:
        cmd = compose.build_normalize_command(
            Path("ffmpeg.exe"), Path("in.mp4"), Path("out.mp4"),
            fade_seconds=0.0, duration=10,
        )
        self.assertNotIn("-af", cmd)

    def test_concat_command_uses_concat_demuxer_and_copy(self) -> None:
        cmd = compose.build_concat_command(Path("ffmpeg.exe"), Path("list.txt"), Path("out.mp4"))
        text = " ".join(cmd)
        self.assertIn("-f concat", text)
        self.assertIn("-safe 0", text)
        self.assertIn("-c copy", text)

    def test_list_file_normalizes_slashes_and_escapes_quotes(self) -> None:
        text = compose.build_list_file([Path("C:/a b/seg'01.mp4")])
        self.assertTrue(text.startswith("file 'C:/a b/"))
        self.assertIn("'\\''", text, "路径里的单引号必须转义，否则 FFmpeg 读不出")


class UniformCheckTest(unittest.TestCase):
    def make(self, name: str, **overrides) -> compose.SegmentInfo:
        base = dict(duration=15.0, width=1376, height=768, fps="24/1",
                    video_codec="h264", audio_codec="aac", sample_rate="32000", channels=2)
        base.update(overrides)
        return compose.SegmentInfo(path=Path(name), **base)

    def test_uniform_segments_pass(self) -> None:
        infos = [self.make("a.mp4"), self.make("b.mp4")]
        self.assertEqual(compose.check_uniform(infos), [])

    def test_resolution_mismatch_is_rejected(self) -> None:
        infos = [self.make("a.mp4"), self.make("b.mp4", width=960, height=544)]
        with self.assertRaises(compose.ComposeError) as ctx:
            compose.check_uniform(infos)
        self.assertIn("分辨率", str(ctx.exception))
        self.assertIn("b.mp4", str(ctx.exception))

    def test_audio_parameter_mismatch_is_rejected(self) -> None:
        infos = [self.make("a.mp4"), self.make("b.mp4", sample_rate="44100")]
        with self.assertRaises(compose.ComposeError) as ctx:
            compose.check_uniform(infos)
        self.assertIn("采样率", str(ctx.exception))

    def test_empty_segment_list_is_rejected(self) -> None:
        with self.assertRaises(compose.ComposeError):
            compose.check_uniform([])

    def test_missing_files_are_reported(self) -> None:
        with self.assertRaises(compose.ComposeError) as ctx:
            compose.compose([Path("no_such_file.mp4")], Path("out.mp4"),
                            ffmpeg=Path("ffmpeg"), ffprobe=Path("ffprobe"))
        self.assertIn("不存在", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
