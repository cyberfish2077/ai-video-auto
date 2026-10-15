"""合成长视频入口（阶段七）。

从项目台账里按顺序取出各分段产物，用 FFmpeg 拼成一条成片。

用法（在项目根目录执行）::

    # 先看看会拼哪些片段（不执行）
    .venv\\Scripts\\python.exe scripts\\compose_final.py --project long30-01 --dry-run

    # 真正合成到 output/final/<项目名>.mp4
    .venv\\Scripts\\python.exe scripts\\compose_final.py --project long30-01

    # 自定义输出路径 / 覆盖已有成片 / 调整音频淡化时长
    .venv\\Scripts\\python.exe scripts\\compose_final.py --project long30-01 ^
        --output output/final/my_cut.mp4 --force --fade 0.12
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
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import compose, config as cfg, logging_util, tasks  # noqa: E402

LOG_NAME = "compose"


def collect_segments(store: tasks.ProjectStore) -> list[tuple[str, Path]]:
    """按台账顺序取出已有产物（已完成或复用的片段）。"""
    project = store.load()
    result: list[tuple[str, Path]] = []
    for seg in project.segments:
        if seg.status not in (tasks.STATUS_DONE, tasks.STATUS_REUSED):
            continue
        if not seg.output_path:
            continue
        result.append((seg.id, Path(seg.output_path)))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="合成长视频（阶段七）")
    parser.add_argument("--project", required=True, help="项目名（读 data/projects/<名字>/segments.json）")
    parser.add_argument("--output", help="成片输出路径，默认 output/final/<项目名>.mp4")
    parser.add_argument("--fade", type=float, default=0.25,
                        help="每段音频淡化时长（秒），默认 0.25（更长则接缝更柔）")
    parser.add_argument("--bgm", help="贯穿全片的背景音乐文件（音频），可选")
    parser.add_argument("--bgm-volume", type=float, default=0.30, help="背景音乐音量（默认 0.30）")
    parser.add_argument("--dissolve", type=float, default=0.0,
                        help="相邻分段交叉溶解时长（秒），0 = 直接拼接（默认 0）；"
                             "推荐 0.4-0.8 让接缝更柔，代价是重编码视频")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有成片")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要拼接的清单")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    store = tasks.ProjectStore(settings.path("paths.projects") / args.project)
    if not store.exists():
        log.error("项目不存在：%s", store.path)
        return 2

    segments = collect_segments(store)
    if not segments:
        log.error("项目里没有可合成的片段（状态需为 done 或 reused）")
        return 2

    print("=" * 72)
    print(f"合成长视频（阶段七）  项目：{args.project}  时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 72)
    total = 0.0
    ffprobe = settings.path("ffmpeg.ffprobe")
    for seg_id, path in segments:
        info = compose.probe_segment(ffprobe, path)
        total += info.duration
        print(f"  {seg_id:10s} {info.duration:6.2f}s  {info.width}x{info.height}  "
              f"{info.video_codec}/{info.audio_codec}  {path.name}")
    print(f"  合计 {len(segments)} 段，预计成片时长 {total:.2f} 秒")

    if args.dry_run:
        print("（dry-run，未执行合成）")
        return 0

    output = (
        Path(args.output)
        if args.output
        else settings.path("paths.projects") / args.project / "final" / f"{args.project}.mp4"
    )
    try:
        result = compose.compose(
            [path for _sid, path in segments],
            output,
            ffmpeg=settings.path("ffmpeg.ffmpeg"),
            ffprobe=ffprobe,
            fade_seconds=args.fade,
            dissolve=args.dissolve,
            bgm=Path(args.bgm) if args.bgm else None,
            bgm_volume=args.bgm_volume,
            force=args.force,
            logger=log,
        )
    except compose.ComposeError as exc:
        log.error("合成失败：%s", exc)
        return 1

    for warning in result.warnings:
        log.warning(warning)
    print()
    log.info("成片：%s", result.output)
    log.info("时长 %.2f 秒｜大小 %.2f MB｜音频淡化 %.0f 毫秒",
             result.duration, result.size_bytes / 1024**2, result.audio_fade * 1000)

    archive_dir = settings.path("paths.projects") / args.project / "compose"
    archive_dir.mkdir(parents=True, exist_ok=True)
    record = archive_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    record.write_text(json.dumps({
        "project": args.project,
        "output": str(result.output),
        "duration": result.duration,
        "size_bytes": result.size_bytes,
        "audio_fade": result.audio_fade,
        "segments": [{"id": sid, "path": str(p)} for sid, p in segments],
        "warnings": result.warnings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("合成记录：%s", record)
    print()
    print("下一步（建议必做）：核对成片里每一段是否就是台账里那一段——")
    print(f"    .venv\\Scripts\\python.exe scripts\\verify_final_segments.py --project {args.project}")
    print("    （2026-10-15 的事故：临时文件名撞名会让成片用错片段，而台账与合成记录都显示正确；"
          "这个脚本按窗口比对画面，能当场抓出来。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
