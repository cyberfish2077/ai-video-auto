"""画面烧录文字（字幕）体检。

背景
----
MiniMax-H3 有时会把提示词里的台词**烧成画面文字**（详见
``references/实测经验与坑.md``）。它是模型行为，提示词与种子都不保证能去掉，
所以本项目的策略是：**接受它的存在，但必须能看见它** —— 每段生成后跑一次体检，
把结论写进项目文档，需要时再决定要不要处理。

原理（2026-10-13 在 long120-01 上实测并验证）
--------------------------------------------
烧进画面的字幕有两条稳定特征：

1. **纯白高亮**：在下部带（默认画面高度 75%→98%）里出现大量灰度 ≥ 阈值的像素；
2. **横排成行**：把那一条带横向切成 16 份，会出现"**中间若干份同时很高**"。

雪光、衣袍高光只会让**单份**局部变亮（实测：seg-03 第 8.5 秒峰值 1.24，
但只有最右侧一份高），所以用第 2 条特征就能把干扰排除掉。

分两层实现，便于测试与复用：

* **纯函数层**（``parse_series`` / ``cluster_candidates`` / ``slice_verdict``）：
  不依赖 ffmpeg，单元测试直接喂数据；
* **探测层**（``check_video`` 等）：调用 ffmpeg 抽帧统计，返回结构化结果。
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("video_auto.textcheck")

#: 下部带：从画面高度 75.5% 起，占 23.1%（1280x736 上即 y=555、高 170）
BAND_TOP_RATIO = 0.755
BAND_HEIGHT_RATIO = 0.231
#: 切片分析用的行带：从 80.2% 起，占 15%（1280x736 上即 y=590、高 110）
SLICE_TOP_RATIO = 0.802
SLICE_HEIGHT_RATIO = 0.15
#: 横向切片数量（越细越容易区分"成行"与"局部高光"）
SLICE_COUNT = 16
#: 只在这些切片里找文字（居中偏左右各留空）：0.3125*16≈5 → 0.6875*16≈11
SLICE_WINDOW = (5, 10)

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_VALUE_RE = re.compile(r"YAVG=([0-9.]+)")


class TextCheckError(RuntimeError):
    """体检过程中的错误，消息里必须带"下一步怎么查"。"""


# --------------------------------------------------------------------------
# 纯函数层（可单元测试）
# --------------------------------------------------------------------------


def parse_series(text: str) -> list[tuple[float, float]]:
    """解析 ffmpeg ``metadata=print`` 的输出，返回 ``[(时间秒, 亮像素占比)]``。

    ffmpeg 会先打印一行 ``pts_time``，紧跟一行 ``YAVG``，两两配对；
    顺序被打乱或数量不匹配时按较短的序列截断（不猜测、不填充）。
    """
    times: list[float] = []
    values: list[float] = []
    for line in text.splitlines():
        hit_time = _PTS_RE.search(line)
        if hit_time:
            times.append(float(hit_time.group(1)))
            continue
        hit_value = _VALUE_RE.search(line)
        if hit_value:
            values.append(float(hit_value.group(1)))
    return list(zip(times, values))


@dataclass
class BandSample:
    """下部带某个时刻的亮像素占比。"""

    time: float
    ratio: float


def cluster_candidates(samples: list[BandSample], *, floor: float, gap: float = 1.0
                       ) -> list[dict]:
    """把连续的超标采样合并成"候选窗口"，每个窗口只留峰值那一个采样。

    这样后续做耗时的切片分析时，只分析窗口峰值即可，既快又稳。
    """
    hits = [s for s in samples if s.ratio > floor]
    windows: list[dict] = []
    current: list[BandSample] = []
    for sample in hits:
        if current and sample.time - current[-1].time > gap:
            windows.append(_window_from(current))
            current = []
        current.append(sample)
    if current:
        windows.append(_window_from(current))
    return windows


def _window_from(samples: list[BandSample]) -> dict:
    peak = max(samples, key=lambda s: s.ratio)
    return {
        "start": samples[0].time,
        "end": samples[-1].time,
        "peak_time": peak.time,
        "peak_ratio": peak.ratio,
    }


def slice_verdict(slices: list[float], *, window: tuple[int, int] = SLICE_WINDOW,
                  slice_floor: float = 5.0, min_slices: int = 3) -> bool:
    """判断横向切片是否呈现"一行字幕"的特征。

    ``slices`` 长度通常是 :data:`SLICE_COUNT`。判定规则：在
    ``window`` 指定的中间切片范围内，亮像素占比 ≥ ``slice_floor`` 的切片数量
    达到 ``min_slices`` 即认为是字幕（实测字幕为连续 4 份，局部高光只有 1 份）。
    """
    if not slices:
        return False
    low, high = window
    low = max(0, low)
    high = min(len(slices) - 1, high)
    return sum(1 for value in slices[low:high + 1] if value >= slice_floor) >= min_slices


# --------------------------------------------------------------------------
# 探测层（需要 ffmpeg / ffprobe）
# --------------------------------------------------------------------------


@dataclass
class TextCheckResult:
    """单个分段的体检结果。"""

    path: Path
    width: int
    height: int
    samples: list[BandSample] = field(default_factory=list)
    windows: list[dict] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """``caption``（发现字幕）或 ``clean``（未发现）。"""
        return "caption" if any(w.get("is_caption") for w in self.windows) else "clean"

    def caption_spans(self) -> list[tuple[float, float]]:
        """疑似字幕的时间段（秒），供"定点修复"直接使用。"""
        return [(w["start"], w["end"]) for w in self.windows if w.get("is_caption")]

    def to_dict(self) -> dict:
        return {
            "file": str(self.path),
            "width": self.width,
            "height": self.height,
            "verdict": self.verdict,
            "samples": len(self.samples),
            "windows": self.windows,
        }


def _run(cmd: list[str], *, what: str, timeout: int = 600) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise TextCheckError(
            f"{what}失败。\n{(proc.stderr or '').strip()[:400]}\n"
            f"解决办法：确认 config/settings.yaml 里的 ffmpeg 路径正确，且该视频能正常播放。"
        )
    return (proc.stdout or "") + (proc.stderr or "")


def probe_size(ffprobe: Path, path: Path) -> tuple[int, int]:
    """读出视频分辨率，用来按比例换算下部带的位置。"""
    out = _run([str(ffprobe), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
               what=f"读取 {path.name} 的分辨率")
    parts = out.strip().split(",")
    if len(parts) < 2 or not parts[0].isdigit():
        raise TextCheckError(
            f"读不出 {path.name} 的分辨率（输出：{out.strip()[:80]}）。\n"
            f"解决办法：确认这是完整的视频文件，必要时重新生成该分段。"
        )
    return int(parts[0]), int(parts[1])


def band_geometry(width: int, height: int) -> tuple[int, int]:
    """下部带（统计亮像素）的 y 与高度。"""
    top = int(height * BAND_TOP_RATIO)
    return top, max(8, int(height * BAND_HEIGHT_RATIO))


def slice_geometry(width: int, height: int) -> tuple[int, int]:
    """切片行带（判断是否成行）的 y 与高度。"""
    top = int(height * SLICE_TOP_RATIO)
    return top, max(8, int(height * SLICE_HEIGHT_RATIO))


def scan_band(ffmpeg: Path, path: Path, *, width: int, height: int,
              fps: float = 4.0, gray_threshold: int = 225) -> list[BandSample]:
    """每 1/fps 秒统计一次下部带里的亮像素占比。"""
    top, band_h = band_geometry(width, height)
    filt = (
        f"fps={fps},crop={width}:{band_h}:0:{top},format=gray,"
        f"lut=y='if(gte(val\\,{gray_threshold})\\,255\\,0)',signalstats,"
        f"metadata=print:key=lavfi.signalstats.YAVG"
    )
    text = _run([str(ffmpeg), "-hide_banner", "-nostats", "-i", str(path),
                 "-vf", filt, "-f", "null", "-"], what=f"扫描 {path.name} 的下部带")
    return [BandSample(t, v) for t, v in parse_series(text)]


def slice_profile(ffmpeg: Path, path: Path, at_time: float, *, width: int, height: int,
                  gray_threshold: int = 225, count: int = SLICE_COUNT) -> list[float]:
    """把 ``at_time`` 那一帧的切片行带横向切成 ``count`` 份，返回各份亮像素占比。"""
    top, band_h = slice_geometry(width, height)
    slice_w = max(8, width // count)
    values: list[float] = []
    with tempfile.TemporaryDirectory(prefix="video_auto_textcheck_") as tmp:
        frame = Path(tmp) / "frame.png"
        _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
              "-ss", f"{at_time:.3f}", "-i", str(path), "-frames:v", "1", str(frame)],
             what=f"抽取 {path.name} 第 {at_time:.2f} 秒的画面")
        if not frame.exists():
            raise TextCheckError(
                f"抽帧失败：{path.name} @ {at_time:.2f}s。\n"
                f"解决办法：确认该时间点在视频时长内。"
            )
        for index in range(count):
            x = index * slice_w
            filt = (
                f"crop={slice_w}:{band_h}:{x}:{top},format=gray,"
                f"lut=y='if(gte(val\\,{gray_threshold})\\,255\\,0)',signalstats,"
                f"metadata=print:key=lavfi.signalstats.YAVG"
            )
            text = _run([str(ffmpeg), "-hide_banner", "-nostats", "-i", str(frame),
                         "-vf", filt, "-f", "null", "-"],
                        what=f"分析 {path.name} 的第 {index} 份切片")
            series = parse_series(text)
            values.append(series[0][1] if series else 0.0)
    return values


def check_video(ffmpeg: Path, ffprobe: Path, path: Path, *, fps: float = 4.0,
                gray_threshold: int = 225, band_floor: float = 1.0,
                slice_floor: float = 5.0, min_slices: int = 3) -> TextCheckResult:
    """体检一个视频：先扫下部带，再对候选窗口的峰值帧做成行判断。"""
    width, height = probe_size(ffprobe, path)
    samples = scan_band(ffmpeg, path, width=width, height=height,
                        fps=fps, gray_threshold=gray_threshold)
    windows = cluster_candidates(samples, floor=band_floor)
    for window in windows:
        slices = slice_profile(ffmpeg, path, window["peak_time"],
                               width=width, height=height, gray_threshold=gray_threshold)
        window["slices"] = [round(v, 2) for v in slices]
        window["is_caption"] = slice_verdict(slices, slice_floor=slice_floor,
                                             min_slices=min_slices)
    return TextCheckResult(path=path, width=width, height=height,
                           samples=samples, windows=windows)


def caption_windows_in_seconds(result: TextCheckResult) -> list[str]:
    """把疑似字幕时段格式化成 ``'12.00-14.50'`` 这种字符串，便于打印/写报告。"""
    return [f"{start:.2f}-{end:.2f}" for start, end in result.caption_spans()]
