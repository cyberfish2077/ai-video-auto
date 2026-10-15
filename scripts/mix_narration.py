"""把旁白混进成片（用户 2026-10-13 选择"旁白跨镜头延续"）。

做法：按视频台账算出每个镜头的成片起点 → 把该镜头的配音摆在这个时间点开口 →
允许它念到后面的镜头里（不截断）→ 与画面自带的环境声混音，说话时环境声自动让路。

硬要求：**两条旁白绝不同时说话**。若某条旁白会盖住下一条的开口时间，默认先给它
轻度加速（不超过 1.2 倍）把时间让出来；还放不下才把下一条推后。这样即使有顺延，
也只会发生在少数几句上，不会让整条旁白轨越漂越远。

用法（在项目根目录）::

    # 只看规划结果，不动文件
    .venv\\Scripts\\python.exe scripts\\mix_narration.py --project jianzhizhe ^
        --narration jianzhizhe-narration --plan

    # 真正混音（默认不覆盖已有文件，加 --force 才覆盖）
    .venv\\Scripts\\python.exe scripts\\mix_narration.py --project jianzhizhe ^
        --narration jianzhizhe-narration --input output/final/jianzhizhe.mp4

    # 先拿几个镜头做小样
    .venv\\Scripts\\python.exe scripts\\mix_narration.py --project jianzhizhe ^
        --narration jianzhizhe-narration --only 01,09,14 --no-video
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import compose, config as cfg, logging_util, tasks  # noqa: E402

LOG_NAME = "mix_narration"


def narration_target(nar_id: str, known_ids) -> str | None:
    """配音 id → 视频台账 id（找不到返回 None）。

    旧项目是编号配对（``nar-07`` ↔ ``seg-07``）；导演系统的镜头 id 是 ``s4`` / ``inh-s1``，
    所以 2026-10-15 起改为**按名字直接匹配**，同时保留编号写法的兼容。
    """
    stem = nar_id[len("nar-"):] if nar_id.startswith("nar-") else nar_id
    for candidate in (stem, f"seg-{stem}", stem.zfill(2), f"seg-{stem.zfill(2)}"):
        if candidate in known_ids:
            return candidate
    return None


def load_entries(store: tasks.ProjectStore) -> list[tuple[str, Path]]:
    """按台账顺序取出已完成的分段（id, 路径）。"""
    project = store.load()
    result: list[tuple[str, Path]] = []
    for seg in project.segments:
        if seg.status not in (tasks.STATUS_DONE, tasks.STATUS_REUSED) or not seg.output_path:
            continue
        path = Path(seg.output_path)
        if path.exists():
            result.append((seg.id, path))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="把旁白混进成片")
    parser.add_argument("--project", required=True, help="视频项目名（提供镜头顺序与时长）")
    parser.add_argument("--narration", required=True, help="配音台账项目名")
    parser.add_argument("--input", help="已合成的成片（不带旁白）；给了才会真正混音")
    parser.add_argument("--output", help="输出路径，默认 output/final/<项目名>_带旁白.mp4")
    parser.add_argument("--mode", choices=["fit", "cascade"], default="fit",
                        help="fit=先轻度加速（默认）；cascade=只顺延不加速")
    parser.add_argument("--gap", type=float, default=0.25, help="两条旁白之间至少留多少秒")
    parser.add_argument("--max-tempo", type=float, default=1.20, help="fit 模式允许的最大加速倍率")
    parser.add_argument("--ambience-db", type=float, default=-3.0,
                        help="不做 ducking 时环境声压低的分贝数")
    parser.add_argument("--voice-gain-db", type=float, default=6.0,
                        help="旁白轨增益（默认 +6 dB）。实测 TTS 峰值 -7.3 dB / H3 环境声峰值 -0.6 dB，"
                             "不加增益旁白会被盖住")
    parser.add_argument("--no-duck", action="store_true", help="关闭 ducking，只压低环境声")
    parser.add_argument("--only", help="只用这些镜头的旁白（逗号分隔），做小样用")
    parser.add_argument("--no-video", action="store_true", help="只产出旁白轨，不与成片混音")
    parser.add_argument("--plan", action="store_true", help="只打印规划，不写任何文件")
    parser.add_argument("--force", action="store_true", help="覆盖已有输出")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    projects_dir = settings.path("paths.projects")
    ffmpeg = settings.path("ffmpeg.ffmpeg")
    ffprobe = settings.path("ffmpeg.ffprobe")

    video_store = tasks.ProjectStore(projects_dir / args.project)
    narration_store = tasks.ProjectStore(projects_dir / args.narration)
    for store, what in ((video_store, "视频台账"), (narration_store, "配音台账")):
        if not store.exists():
            log.error("找不到%s：%s", what, store.path)
            return 2

    clips = load_entries(video_store)
    if not clips:
        log.error("视频台账里没有已完成的分段：%s", video_store.path)
        return 2
    starts: dict[str, float] = {}
    total = 0.0
    for seg_id, path in clips:
        starts[seg_id] = total
        total += compose.probe_segment(ffprobe, path).duration

    wanted = {x.strip() for x in args.only.split(",")} if args.only else None
    picked: list[tuple[str, Path]] = []
    for seg_id, path in load_entries(narration_store):
        target = narration_target(seg_id, starts)
        stem = seg_id[len("nar-"):] if seg_id.startswith("nar-") else seg_id
        if wanted is not None and stem not in wanted and target not in wanted:
            continue
        if target is None:
            log.warning("配音 %s 对应的镜头还没生成（或 id 对不上），先跳过", seg_id)
            continue
        picked.append((target, path))
    if not picked:
        log.error("没有任何可摆放的旁白（检查 --only 或视频台账状态）")
        return 2

    shot_starts = [starts[shot] for shot, _ in picked]
    durations = [compose.probe_segment(ffprobe, path).duration for _, path in picked]
    placements = compose.plan_narration(
        shot_starts, durations, total_seconds=total, gap=args.gap,
        mode=args.mode, max_tempo=args.max_tempo)

    print("=" * 78)
    print(f"旁白规划（{args.mode} 模式）  镜头 {len(clips)} 段 / {total:.2f} 秒，旁白 {len(picked)} 条")
    print("=" * 78)
    print(f"{'镜头':>6} {'镜头起点':>9} {'旁白摆位':>9} {'时长':>7} {'加速':>6} {'延迟':>7}")
    for (shot, path), item in zip(picked, placements):
        flag = "" if abs(item.tempo - 1.0) < 1e-3 else f"{item.tempo:.2f}x"
        over = "" if abs(item.drift) < 0.05 else "  ← 被推后"
        print(f"{shot:>6} {item.shot_start:>8.2f}s {item.start:>8.2f}s {item.duration:>6.2f}s "
              f"{flag:>6} {item.drift:>6.2f}s{over}")
    accelerated = sum(1 for item in placements if item.tempo > 1.001)
    pushed = sum(1 for item in placements if item.drift > 0.05)
    last_end = max(item.start + item.final_duration for item in placements)
    print(f"\n汇总：加速 {accelerated} 条、被推后 {pushed} 条，旁白最晚结束于 {last_end:.1f}s"
          f"（成片 {total:.1f}s）")
    if last_end > total:
        print("[警告] 有旁白会念到成片结束之后，需要删句子或提高 --max-tempo。")
    if args.plan:
        print("（--plan：没有写任何文件）")
        return 0

    work_dir = projects_dir / args.project / "compose"
    work_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    track = work_dir / f"narration-{stamp}.wav"
    clip_args = [(item.start, item.tempo, path) for (_, path), item in zip(picked, placements)]
    cmd = compose.build_narration_track_command(ffmpeg, clip_args, track,
                                                total_seconds=total)
    log.info("生成旁白轨：%s", track.name)
    compose.run_ffmpeg(cmd, timeout=3600, what="生成旁白轨")
    info = compose.probe_segment(ffprobe, track)
    print(f"旁白轨：{track}（{info.duration:.2f} 秒）")

    if args.no_video or not args.input:
        print("（未给 --input 或指定了 --no-video：只产出旁白轨，不混音）")
        return 0

    source = Path(args.input)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    if not source.exists():
        log.error("找不到成片：%s", source)
        return 2
    output = Path(args.output) if args.output else PROJECT_ROOT / "output" / "final" / f"{args.project}_带旁白.mp4"
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    if output.exists() and not args.force:
        log.error("输出已存在：%s（要覆盖请加 --force）", output)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = compose.build_narration_mix_command(
        ffmpeg, source, track, output,
        ambience_db=args.ambience_db, voice_gain_db=args.voice_gain_db,
        duck=not args.no_duck)
    log.info("混音：%s + %s -> %s", source.name, track.name, output.name)
    compose.run_ffmpeg(cmd, timeout=3600, what="旁白混音")
    final_info = compose.probe_segment(ffprobe, output)
    print(f"成片（带旁白）：{output}（{final_info.duration:.2f} 秒，"
          f"{output.stat().st_size / 1024 ** 2:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
