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

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import mix_narration  # noqa: E402


class CommandBuildTest(unittest.TestCase):
    def test_normalized_tmp_path_is_unique_per_segment(self) -> None:
        """不同项目里"沿用"来的片段可能与本片片段同名，临时文件名必须逐段唯一。

        2026-10-15 实测踩过：用文件名主干命名时，上部的 `s2_00001-audio.mp4` 与本片的同名，
        后者覆盖前者 → 成片开头的第 2、3 个镜头被本片的同号段顶掉。
        """
        tmp = Path("C:/tmp/compose")
        first = compose.normalized_tmp_path(tmp, 1, Path("D:/p1/segments/s2_00001-audio.mp4"))
        second = compose.normalized_tmp_path(tmp, 2, Path("D:/p2/segments/s2_00001-audio.mp4"))
        self.assertNotEqual(first, second)
        self.assertIn("01", first.name)
        self.assertIn("02", second.name)
        same = compose.normalized_tmp_path(tmp, 3, Path("D:/p2/segments/s3_00001-audio.mp4"))
        self.assertNotEqual(second, same)

    def test_narration_target_matches_by_name_and_by_number(self) -> None:
        """配音 id 要能对上两种视频台账 id：导演系统的 s4 / inh-s1，以及旧项目的 seg-07。"""
        known = {"s4", "inh-s1", "seg-07"}
        self.assertEqual("s4", mix_narration.narration_target("nar-s4", known))
        self.assertEqual("inh-s1", mix_narration.narration_target("nar-inh-s1", known))
        self.assertEqual("seg-07", mix_narration.narration_target("nar-07", known))
        self.assertIsNone(mix_narration.narration_target("nar-99", known))

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


class NarrationPlanTest(unittest.TestCase):
    """旁白摆放规划：硬要求是"两条旁白绝不同时说话"。"""

    def test_short_lines_keep_their_own_shot_start(self) -> None:
        starts = [0.0, 8.0, 16.0]
        plan = compose.plan_narration(starts, [5.0, 4.0, 6.0], total_seconds=24.0)
        self.assertEqual([round(item.start, 2) for item in plan], [0.0, 8.0, 16.0])
        self.assertTrue(all(item.tempo == 1.0 for item in plan))

    def test_long_line_is_slightly_accelerated_in_fit_mode(self) -> None:
        # 第一句 9.6 秒，但下一条 8 秒就要开口 → 让出 0.25 秒间隙需要 1.24 倍，
        # 上限 1.20 → 还差一点，于是下一条被推后
        plan = compose.plan_narration([0.0, 8.0], [9.6, 3.0], total_seconds=16.0,
                                      gap=0.25, mode="fit", max_tempo=1.20)
        self.assertAlmostEqual(plan[0].tempo, 1.20, places=3)
        self.assertGreater(plan[1].start, 8.0, "放不下时下一条要被推后")
        self.assertGreaterEqual(plan[1].start - (plan[0].start + plan[0].final_duration), 0.0)

    def test_fit_mode_can_make_it_without_pushing(self) -> None:
        plan = compose.plan_narration([0.0, 8.0], [8.5, 3.0], total_seconds=16.0,
                                      gap=0.25, mode="fit", max_tempo=1.20)
        self.assertGreater(plan[0].tempo, 1.0)
        self.assertAlmostEqual(plan[1].start, 8.0, places=3)

    def test_cascade_mode_never_accelerates(self) -> None:
        plan = compose.plan_narration([0.0, 8.0], [9.6, 3.0], total_seconds=16.0,
                                      gap=0.25, mode="cascade")
        self.assertEqual([item.tempo for item in plan], [1.0, 1.0])
        self.assertAlmostEqual(plan[1].start, 9.85, places=2)

    def test_no_overlap_between_any_two_lines(self) -> None:
        starts = [i * 8.0 for i in range(6)]
        durations = [9.5, 7.0, 10.8, 6.0, 11.0, 5.0]
        for mode in ("fit", "cascade"):
            plan = compose.plan_narration(starts, durations, total_seconds=48.0, mode=mode)
            for previous, current in zip(plan, plan[1:]):
                self.assertLessEqual(previous.start + previous.final_duration, current.start + 1e-6,
                                     f"{mode} 模式下第 {previous.index} 与 {current.index} 条重叠了")

    def test_narration_track_command_has_delay_and_tempo(self) -> None:
        cmd = compose.build_narration_track_command(
            Path("ffmpeg"),
            [(0.0, 1.0, Path("a.mp3")), (8.0, 1.15, Path("b.mp3"))],
            Path("vo.wav"), total_seconds=24.0)
        joined = " ".join(cmd)
        self.assertIn("adelay=0|0", joined)
        self.assertIn("adelay=8000|8000", joined)
        self.assertIn("atempo=1.1500", joined)
        self.assertIn("amix=inputs=2", joined)
        self.assertIn("atrim=0:24.000", joined)

    def test_narration_mix_command_ducks_ambience(self) -> None:
        cmd = compose.build_narration_mix_command(
            Path("ffmpeg"), Path("v.mp4"), Path("vo.wav"), Path("out.mp4"), duck=True)
        joined = " ".join(cmd)
        self.assertIn("sidechaincompress", joined)
        self.assertIn("-map", cmd)
        plain = " ".join(compose.build_narration_mix_command(
            Path("ffmpeg"), Path("v.mp4"), Path("vo.wav"), Path("out.mp4"),
            duck=False, ambience_db=-6.0))
        self.assertNotIn("sidechaincompress", plain)
        self.assertIn("volume=-6.0dB", plain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
