"""FFmpeg 合成模块（阶段七）：把多个分段视频拼成一条成片。

关键设计
--------
* H3 生成的每一段**自带音轨**。直接首尾相接，在接缝处会有"咔哒"爆音，
  所以每段音频先加极短的淡入淡出（默认 250 毫秒）再拼接；
* 可选**交叉溶解**（``dissolve``）：相邻两段在画面与声音上重叠一小段，
  接缝最柔；代价是要重编码视频（比直拷慢），适合分段之间内容有轻微跳变时；
* 可选**贯穿全片的背景音乐**（``bgm``）：分段各自生成的音乐会在此接缝处断掉。
  做法是提示词里要求分段**不生成 BGM**，拼接后把一条完整的 BGM 铺满整片。
* 可选**旁白轨**：逐条配音按各自镜头的时间点摆好（``plan_narration``），
  允许多念出来的部分延续到后面的镜头里；混音时用 sidechaincompress 让环境声
  在旁白说话时自动让路（ducking），保证听清。
* 拼接前先校验各段规格一致（编码/分辨率/帧率/采样率），不一致直接报错，
  而不是产出一条画面忽大忽小、音画不同步的成片；
* 默认**不覆盖已有成片**。

命令构造与执行分开，便于单元测试（不需要真的装 FFmpeg 就能验证参数）。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class ComposeError(RuntimeError):
    """合成失败，错误信息需说明原因与下一步动作。"""


@dataclass
class SegmentInfo:
    path: Path
    duration: float
    width: int | None = None
    height: int | None = None
    fps: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: str | None = None
    channels: int | None = None

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)


@dataclass
class ComposeResult:
    output: Path
    duration: float
    size_bytes: int
    segments: list[SegmentInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audio_fade: float = 0.0
    dissolve: float = 0.0
    used_bgm: bool = False


# --------------------------------------------------------------------------
# 探测与校验
# --------------------------------------------------------------------------


def probe_segment(ffprobe: Path, path: Path) -> SegmentInfo:
    """读取单个分段的规格（时长、分辨率、帧率、编解码、采样率）。"""
    cmd = [
        str(ffprobe), "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    if proc.returncode != 0:
        raise ComposeError(
            f"ffprobe 无法读取：{path}\n{(proc.stderr or '').strip()[:300]}\n"
            f"解决办法：确认文件未损坏，必要时重新生成该分段。"
        )
    data = json.loads(proc.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    return SegmentInfo(
        path=path,
        duration=float((data.get("format") or {}).get("duration") or 0.0),
        width=video.get("width"),
        height=video.get("height"),
        fps=video.get("r_frame_rate"),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
        sample_rate=audio.get("sample_rate"),
        channels=audio.get("channels"),
    )


def check_uniform(infos: list[SegmentInfo]) -> list[str]:
    """校验各段规格是否一致；不一致直接报错，避免拼出坏成片。"""
    if not infos:
        raise ComposeError("没有可合成的分段。")
    first = infos[0]
    problems: list[str] = []
    for info in infos[1:]:
        for name, attr in (("分辨率", ("width", "height")), ("视频编码", ("video_codec",)),
                           ("帧率", ("fps",)), ("音频编码", ("audio_codec",)),
                           ("采样率", ("sample_rate",)), ("声道数", ("channels",))):
            a = tuple(getattr(first, key) for key in attr)
            b = tuple(getattr(info, key) for key in attr)
            if a != b:
                problems.append(f"{info.path.name} 的{name}与首段不一致：{b} vs {a}")
    if problems:
        raise ComposeError(
            "各分段规格不一致，直接拼接会出问题：\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n解决办法：用相同参数重新生成这些分段；或先把它们统一转码（本模块暂不自动转码）。"
        )
    return []


# --------------------------------------------------------------------------
# 命令构造（与执行分开，便于测试）
# --------------------------------------------------------------------------


def build_normalize_command(
    ffmpeg: Path,
    src: Path,
    dst: Path,
    *,
    fade_seconds: float,
    duration: float,
    audio_bitrate: str = "192k",
) -> list[str]:
    """构造"视频直拷 + 音频加重淡入淡出"的命令。"""
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(src), "-c:v", "copy"]
    filters: list[str] = []
    if fade_seconds > 0:
        filters.append(f"afade=t=in:st=0:d={fade_seconds}")
        out_start = max(0.0, duration - fade_seconds)
        filters.append(f"afade=t=out:st={out_start:.3f}:d={fade_seconds}")
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-c:a", "aac", "-b:a", audio_bitrate, str(dst)]
    return cmd


def build_concat_command(ffmpeg: Path, list_file: Path, output: Path) -> list[str]:
    """构造"按清单直接拼接"的命令（不重编码）。"""
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(output),
    ]


def build_mix_command(
    ffmpeg: Path,
    video: Path,
    bgm: Path,
    output: Path,
    *,
    duration: float,
    bgm_volume: float = 0.30,
    fade_in: float = 2.0,
    fade_out: float = 3.0,
    audio_bitrate: str = "192k",
) -> list[str]:
    """构造"把一条完整 BGM 铺满整片"的命令（视频直拷、音频重混）。"""
    out_start = max(0.0, duration - fade_out)
    filters = (
        f"[1:a]volume={bgm_volume},"
        f"afade=t=in:st=0:d={fade_in},"
        f"afade=t=out:st={out_start:.3f}:d={fade_out}[bgm];"
        # normalize=0 保持原音量，否则 amix 会把对白压低一半
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];"
        f"[mix]alimiter=limit=0.95[aout]"
    )
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(video),
        "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", filters,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart", str(output),
    ]


def build_dissolve_command(
    ffmpeg: Path,
    inputs: list[Path],
    output: Path,
    *,
    dissolve: float,
    durations: list[float],
    crf: int = 18,
    preset: str = "medium",
    audio_bitrate: str = "192k",
) -> list[str]:
    """构造"分段之间交叉溶解"的命令：视频 xfade + 音频 acrossfade。

    dissolve 秒的重叠让画面与声音都平滑过渡；代价是要重编码视频。
    """
    parts: list[str] = []
    v_prev, a_prev = "[0:v]", "[0:a]"
    offset = durations[0] - dissolve
    for index in range(1, len(inputs)):
        v_out, a_out = f"[v{index}]", f"[a{index}]"
        parts.append(
            f"{v_prev}[{index}:v]xfade=transition=fade:duration={dissolve:.3f}"
            f":offset={max(offset, 0.0):.3f}{v_out}"
        )
        parts.append(f"{a_prev}[{index}:a]acrossfade=d={dissolve:.3f}:c1=tri:c2=tri{a_out}")
        offset += durations[index] - dissolve
        v_prev, a_prev = v_out, a_out

    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", v_prev, "-map", a_prev,
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart", str(output),
    ]
    return cmd


def normalized_tmp_path(tmp_dir: Path, index: int, src: Path) -> Path:
    """规范化后的临时文件路径：**必须逐段唯一**。

    2026-10-15 实测踩过这个坑：原来用 ``norm_{src.stem}.mp4`` 命名，而"沿用上一部片"的片段
    与本片片段**文件名主干相同**（两边的 ``s2_00001-audio.mp4``），于是后写的覆盖先写的，
    concat 清单里同一个文件出现两次——沿用段的第 2、3 个镜头被本片的同号段顶掉，
    成片开头因此重播了本片的 s2／s3。加上序号前缀后，路径与"第几段"绑死，不再可能撞名。
    """
    return tmp_dir / f"norm_{index:02d}_{src.stem}.mp4"


def build_list_file(entries: list[Path]) -> str:
    """生成 concat 清单内容（路径里的单引号需要转义）。"""
    lines = []
    for path in entries:
        text = str(path).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{text}'")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 旁白（配音）时间轴：把逐条配音摆到各自镜头的时间点上
# --------------------------------------------------------------------------


@dataclass
class NarrationPlacement:
    """一条旁白最终摆在哪里、要不要加速。"""

    index: int
    shot_start: float      # 该镜头在成片里的起始时间
    start: float           # 实际摆放时间（cascade 模式下可能被推后）
    tempo: float           # 速度倍率（1.0 = 不加速）
    duration: float        # 原始时长

    @property
    def final_duration(self) -> float:
        return self.duration / self.tempo

    @property
    def drift(self) -> float:
        """相对它自己镜头起点的延迟（越大越"跑题"）。"""
        return self.start - self.shot_start


def plan_narration(
    shot_starts: list[float],
    durations: list[float],
    *,
    total_seconds: float,
    gap: float = 0.25,
    mode: str = "fit",
    max_tempo: float = 1.20,
) -> list[NarrationPlacement]:
    """规划每条旁白的位置。

    两种策略（用户 2026-10-13 选择"旁白跨镜头延续"，即允许旁白念到下一个镜头里）：

    * ``fit``：旁白按自己镜头的起点开口；若它会盖住下一条旁白的开口时间，
      先给它做**轻度加速**（不超过 ``max_tempo``）把时间让出来，还是放不下才把下一条推后；
    * ``cascade``：一律不加速，念完为止，下一条往后顺延。

    无论哪种，**两条旁白都不会同时说话** —— 这是硬要求，重叠会让人听不清。
    """
    placements: list[NarrationPlacement] = []
    previous_end = -1e9
    for index, (shot_start, duration) in enumerate(zip(shot_starts, durations)):
        next_start = shot_starts[index + 1] if index + 1 < len(shot_starts) else total_seconds
        start = max(shot_start, previous_end + gap)
        available = max(next_start, start + 0.1) - start - gap
        tempo = 1.0
        if mode == "fit" and duration > available > 0:
            tempo = min(max_tempo, duration / available)
        final = duration / tempo
        placements.append(NarrationPlacement(index=index, shot_start=shot_start, start=start,
                                            tempo=tempo, duration=duration))
        previous_end = start + final
    return placements


def build_narration_track_command(
    ffmpeg: Path,
    clips: list[tuple[float, float, Path]],
    output: Path,
    *,
    total_seconds: float,
    sample_rate: int = 32000,
) -> list[str]:
    """把逐条旁白拼成一条完整的旁白轨（其余时间留白）。

    ``clips`` 里每项是 ``(摆放时间, 速度倍率, 音频文件)``。
    输出未压缩 WAV，留给最后一步与画面声混音（避免二次压缩）。
    """
    if not clips:
        raise ComposeError("旁白清单是空的，无法生成旁白轨。")
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
    for _start, _tempo, path in clips:
        cmd += ["-i", str(path)]
    parts: list[str] = []
    labels: list[str] = []
    for index, (start, tempo, _path) in enumerate(clips):
        chain = f"[{index}:a]aresample={sample_rate}"
        if abs(tempo - 1.0) > 1e-3:
            # atempo 一次只接受 0.5~2.0，本项目倍率都很小，单次足够
            chain += f",atempo={tempo:.4f}"
        chain += f",adelay={int(round(start * 1000))}|{int(round(start * 1000))}[n{index}]"
        parts.append(chain)
        labels.append(f"[n{index}]")
    parts.append(
        "".join(labels) +
        f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        f"apad,atrim=0:{total_seconds:.3f}[vo]"
    )
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[vo]",
        "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "2",
        str(output),
    ]
    return cmd


def build_narration_mix_command(
    ffmpeg: Path,
    video: Path,
    narration: Path,
    output: Path,
    *,
    ambience_db: float = -3.0,
    voice_gain_db: float = 0.0,
    duck: bool = True,
    audio_bitrate: str = "192k",
) -> list[str]:
    """把旁白轨混进成片：压低环境声（或做 ducking），旁白保持清晰。

    ``duck=True`` 时用 sidechaincompress 做"旁白一响，环境声自动让路"，
    旁白间隙环境声回到原音量；否则只把环境声整体压低 ``ambience_db`` 分贝。

    ``voice_gain_db`` 用来补足 TTS 与 H3 环境声之间的电平差（实测旁白峰值 -7.3 dB、
    环境声峰值 -0.6 dB，不改就会被盖住）。
    """
    voice = f"[1:a]aresample=32000"
    if abs(voice_gain_db) > 1e-3:
        voice += f",volume={voice_gain_db}dB"
    voice += "[vo]"
    if duck:
        filters = (
            f"[0:a]aresample=32000,volume={ambience_db}dB[amb];"
            f"{voice};"
            f"[vo]asplit=2[vo_main][vo_key];"
            f"[amb][vo_key]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[amb_duck];"
            f"[amb_duck][vo_main]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];"
            f"[mix]alimiter=limit=0.95[aout]"
        )
    else:
        filters = (
            f"[0:a]aresample=32000,volume={ambience_db}dB[amb];"
            f"{voice};"
            f"[amb][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];"
            f"[mix]alimiter=limit=0.95[aout]"
        )
    return [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(video),
        "-i", str(narration),
        "-filter_complex", filters,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart", str(output),
    ]


# --------------------------------------------------------------------------
# 合成主流程
# --------------------------------------------------------------------------


def _run(cmd: list[str], *, timeout: int, what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise ComposeError(
            f"{what}失败。\n{(proc.stderr or '').strip()[-500:]}\n"
            f"解决办法：确认 config/settings.yaml 里的 ffmpeg 路径正确，且各分段规格一致。"
        )


#: 公开别名：别的脚本（如 scripts/mix_narration.py）需要执行 ffmpeg 命令
run_ffmpeg = _run


def compose(
    segments: list[Path],
    output: Path,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    fade_seconds: float = 0.25,
    dissolve: float = 0.0,
    bgm: Path | None = None,
    bgm_volume: float = 0.30,
    crf: int = 18,
    preset: str = "medium",
    force: bool = False,
    logger: logging.Logger | None = None,
) -> ComposeResult:
    """把 ``segments`` 按顺序拼成 ``output``。

    * ``dissolve > 0``：交叉溶解（画面+声音重叠），接缝最柔，需重编码；
    * ``bgm``：拼接后把一条完整 BGM 铺满整片（分段应已要求不生成 BGM）。
    """
    log = logger or logging.getLogger("video_auto.compose")
    if not segments:
        raise ComposeError("没有可合成的分段。")
    missing = [p for p in segments if not p.exists()]
    if missing:
        raise ComposeError(
            "以下分段文件不存在：\n" + "\n".join(f"  - {p}" for p in missing)
            + "\n解决办法：先跑生成流程，或检查项目台账里的产物路径。"
        )
    if output.exists() and not force:
        raise ComposeError(
            f"成片已存在，未覆盖：{output}\n"
            f"解决办法：改名输出（--output），或加 --force 明确覆盖。"
        )
    if bgm and not bgm.exists():
        raise ComposeError(
            f"指定的背景音乐不存在：{bgm}\n"
            f"解决办法：检查 --bgm 路径，或去掉该参数（则只用分段自带音轨）。"
        )

    infos = [probe_segment(ffprobe, p) for p in segments]
    warnings = check_uniform(infos)
    if any(not info.has_audio for info in infos):
        raise ComposeError(
            "有分段没有音轨，无法按「拼接音轨」的方式合成：\n"
            + "\n".join(f"  - {i.path.name}" for i in infos if not i.has_audio)
            + "\n解决办法：检查生成工作流是否输出音频（H3 默认会生成音轨）。"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video_auto_compose_") as tmp:
        tmp_dir = Path(tmp)
        joined = tmp_dir / "joined.mp4"

        if dissolve > 0 and len(infos) > 1:
            log.info("交叉溶解拼接 %d 个分段（重叠 %.2f 秒）", len(infos), dissolve)
            _run(
                build_dissolve_command(
                    ffmpeg, [i.path for i in infos], joined,
                    dissolve=dissolve, durations=[i.duration for i in infos],
                    crf=crf, preset=preset,
                ),
                timeout=5400, what="交叉溶解拼接",
            )
        else:
            normalized: list[Path] = []
            for index, info in enumerate(infos, start=1):
                dst = normalized_tmp_path(tmp_dir, index, info.path)
                log.info("处理音轨：%s（淡入淡出 %.0f 毫秒）", info.path.name, fade_seconds * 1000)
                _run(
                    build_normalize_command(ffmpeg, info.path, dst,
                                            fade_seconds=fade_seconds, duration=info.duration),
                    timeout=1800, what=f"处理 {info.path.name} 的音轨",
                )
                normalized.append(dst)
            list_file = tmp_dir / "concat_list.txt"
            list_file.write_text(build_list_file(normalized), encoding="utf-8")
            log.info("拼接 %d 个分段（直接拼接）", len(normalized))
            _run(build_concat_command(ffmpeg, list_file, joined), timeout=3600, what="拼接")

        if not joined.exists():
            raise ComposeError("拼接没有产出文件。\n解决办法：查看上面 ffmpeg 的输出。")

        if bgm:
            joined_info = probe_segment(ffprobe, joined)
            log.info("铺入贯穿全片的背景音乐（音量 %.2f）：%s", bgm_volume, bgm.name)
            _run(
                build_mix_command(ffmpeg, joined, bgm, output,
                                  duration=joined_info.duration, bgm_volume=bgm_volume),
                timeout=3600, what="混入背景音乐",
            )
        else:
            shutil.copy2(joined, output)

    final = probe_segment(ffprobe, output)
    overlap = dissolve * (len(infos) - 1) if dissolve > 0 else 0.0
    expected = sum(i.duration for i in infos) - overlap
    if abs(final.duration - expected) > max(0.5, expected * 0.03):
        warnings.append(
            f"成片时长 {final.duration:.2f} 秒与预期 {expected:.2f} 秒差距偏大，请人工确认。"
        )
    log.info("合成完成：%.2f 秒 / %.2f MB", final.duration, output.stat().st_size / 1024**2)
    return ComposeResult(
        output=output, duration=final.duration, size_bytes=output.stat().st_size,
        segments=infos, warnings=warnings, audio_fade=fade_seconds,
        dissolve=dissolve, used_bgm=bool(bgm),
    )
