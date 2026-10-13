"""textcheck 纯逻辑层单元测试（不需要 ffmpeg，不占显卡）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_auto import textcheck  # noqa: E402

#: 实测：seg-04 第 12.0 秒（画面里烧着「那一夜 也下着雪」）
CAPTION_SLICES = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 15.3, 14.5, 14.0, 18.5, 0.0,
                  0.0, 0.0, 0.0, 0.0, 0.0]
#: 实测：seg-03 第 8.5 秒（只有最右侧条高，是雪面/衣袍高光，不是字）
HIGHLIGHT_SLICES = [0.3, 1.5, 1.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.4,
                    11.4, 1.1, 0.1, 0.0, 0.0]
#: 实测：seg-06 第 12.0 秒（台词镜头，但全程没有字）
CLEAN_SLICES = [0.0] * 16


class ParseSeriesTest(unittest.TestCase):
    def test_pairs_pts_time_with_value(self) -> None:
        text = (
            "[Parsed_ametadata_1 @ 000] frame:0    pts:0       pts_time:0\n"
            "[Parsed_ametadata_1 @ 000] lavfi.signalstats.YAVG=0.00\n"
            "[Parsed_ametadata_1 @ 000] frame:1    pts:6       pts_time:0.25\n"
            "[Parsed_ametadata_1 @ 000] lavfi.signalstats.YAVG=2.48\n"
        )
        self.assertEqual(textcheck.parse_series(text), [(0.0, 0.0), (0.25, 2.48)])

    def test_ignores_unrelated_lines(self) -> None:
        text = "some noise\nstream mapping\n"
        self.assertEqual(textcheck.parse_series(text), [])


class ClusterCandidatesTest(unittest.TestCase):
    def test_groups_consecutive_hits_and_keeps_peak(self) -> None:
        samples = [
            textcheck.BandSample(0.0, 0.0),
            textcheck.BandSample(1.0, 2.0),
            textcheck.BandSample(1.25, 2.6),
            textcheck.BandSample(1.5, 2.4),
            textcheck.BandSample(5.0, 0.2),
            textcheck.BandSample(7.0, 3.9),
        ]
        windows = textcheck.cluster_candidates(samples, floor=1.0)
        self.assertEqual(len(windows), 2, "中间隔了 3 秒以上应当算两个窗口")
        self.assertAlmostEqual(windows[0]["peak_time"], 1.25)
        self.assertAlmostEqual(windows[0]["peak_ratio"], 2.6)
        self.assertAlmostEqual(windows[1]["start"], 7.0)

    def test_floors_out_weak_signals(self) -> None:
        samples = [textcheck.BandSample(0.5, 0.76), textcheck.BandSample(1.0, 0.1)]
        self.assertEqual(textcheck.cluster_candidates(samples, floor=1.0), [])


class SliceVerdictTest(unittest.TestCase):
    def test_centered_row_is_caption(self) -> None:
        self.assertTrue(textcheck.slice_verdict(CAPTION_SLICES))

    def test_single_side_highlight_is_not_caption(self) -> None:
        self.assertFalse(textcheck.slice_verdict(HIGHLIGHT_SLICES))

    def test_clean_frame_is_not_caption(self) -> None:
        self.assertFalse(textcheck.slice_verdict(CLEAN_SLICES))

    def test_empty_slices_are_safe(self) -> None:
        self.assertFalse(textcheck.slice_verdict([]))

    def test_edge_slices_do_not_count(self) -> None:
        # 左右两侧的高亮（例如画面边缘的灯火）不应被当成字幕
        slices = [9.0, 9.0, 9.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                  0.0, 9.0, 9.0, 9.0]
        self.assertFalse(textcheck.slice_verdict(slices))


class GeometryTest(unittest.TestCase):
    def test_project_resolution_geometry(self) -> None:
        """1280x736 上应当复现取证时用的两条带（y=555 高 170；y=590 高 110）。"""
        self.assertEqual(textcheck.band_geometry(1280, 736), (555, 170))
        self.assertEqual(textcheck.slice_geometry(1280, 736), (590, 110))


if __name__ == "__main__":
    unittest.main()
