"""分段画面"烧录字幕"体检（阶段八附属工具，不占用显卡）。

用法（在项目根目录执行）::

    # 体检一个项目里所有已完成的分段
    .venv\\Scripts\\python.exe scripts\\check_segment_text.py --project long120-01

    # 只体检某一段，并把结论写进 projects/<项目>/analysis/
    .venv\\Scripts\\python.exe scripts\\check_segment_text.py --project long120-01 ^
        --id seg-04 --write-report

    # 体检任意一个视频文件
    .venv\\Scripts\\python.exe scripts\\check_segment_text.py ^
        --file projects\\long120-01\\segments\\seg-04_00002-audio.mp4

退出码：0 = 都没发现字幕；1 = 有分段发现字幕；2 = 出错（信息里带下一步动作）。

判定方法见 ``src/video_auto/textcheck.py`` 的模块注释：下部带亮像素 +
横向切片是否"中间成行"。经验值：字幕会让峰值升到 2~5，局部高光一般 <1.2 或只占单条切片。
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

from video_auto import config as cfg, logging_util, tasks, textcheck  # noqa: E402

LOG_NAME = "textcheck"


def collect_targets(args, settings, log) -> list[Path]:
    """整理出要体检的文件清单：--file 优先，否则按项目台账取已完成的分段。"""
    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            log.error("文件不存在：%s", path)
            return []
        return [path]

    store = tasks.ProjectStore(settings.path("paths.projects") / args.project)
    if not store.exists():
        log.error("项目不存在：%s（检查项目名，或改用 --file 指定视频）", store.path)
        return []
    project = store.load()
    wanted = None
    if args.id:
        wanted = {piece.strip() for piece in args.id.split(",") if piece.strip()}
    paths: list[Path] = []
    for seg in project.segments:
        if wanted and seg.id not in wanted:
            continue
        if seg.status not in (tasks.STATUS_DONE, tasks.STATUS_REUSED):
            continue
        if not seg.output_path:
            continue
        path = Path(seg.output_path)
        if not path.exists():
            log.warning("%s 的产物不在磁盘上，跳过：%s", seg.id, path)
            continue
        paths.append(path)
    if wanted:
        found = {p.stem.split("_")[0] for p in paths}
        missing = wanted - found
        if missing:
            log.error("台账里找不到这些片段（或它们还没生成）：%s", "、".join(sorted(missing)))
    if not paths:
        log.error("没有可体检的分段（项目里没有已完成的分段？请先跑 run_batch.py run）")
    return paths


def format_result(result: textcheck.TextCheckResult) -> str:
    """把一份体检结果整理成给人看的多行文本。"""
    lines = [f"  {result.path.name}  {result.width}x{result.height}"]
    if not result.windows:
        lines.append(f"    结论：干净（{len(result.samples)} 个采样点，"
                     f"下部带最高亮像素占比 "
                     f"{max((s.ratio for s in result.samples), default=0.0):.2f}）")
        return "\n".join(lines)
    for window in result.windows:
        mark = "■ 字幕" if window.get("is_caption") else "○ 疑似高光"
        lines.append(
            f"    {mark}  {window['start']:.2f}-{window['end']:.2f}s"
            f"（峰值 {window['peak_ratio']:.2f} @ {window['peak_time']:.2f}s）"
        )
        lines.append(f"       横向切片：{window.get('slices')}")
    spans = textcheck.caption_windows_in_seconds(result)
    if spans:
        lines.append(f"    结论：发现字幕，时间段 {'、'.join(spans)}")
        lines.append("       备用处理：ffmpeg delogo 定点修复，参数见 "
                     "references/实测经验与坑.md 第七节")
    else:
        lines.append("    结论：干净（有高亮，但都是局部高光，不成行）")
    return "\n".join(lines)


def write_report(settings, project_name: str, results: list, log) -> Path:
    """把体检结论写到 projects/<项目>/analysis/字幕体检-<时间>.md。"""
    out_dir = settings.path("paths.projects") / project_name / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"字幕体检-{stamp}.md"
    captions = [r for r in results if r.verdict == "caption"]
    lines = [
        f"# 字幕体检报告（{project_name}）",
        "",
        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}　"
        f"工具：`scripts/check_segment_text.py`　"
        f"方法：下部带亮像素 + 横向 16 切片成行判断",
        "",
        f"**结论**：体检 {len(results)} 段，发现字幕 {len(captions)} 段。",
        "",
        "| 分段 | 结论 | 疑似字幕时间段 | 峰值 |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        spans = "、".join(textcheck.caption_windows_in_seconds(result)) or "—"
        peak = max((s.ratio for s in result.samples), default=0.0)
        verdict = "发现字幕" if result.verdict == "caption" else "干净"
        lines.append(f"| {result.path.name} | {verdict} | {spans} | {peak:.2f} |")
    lines += [
        "",
        "> 说明：字幕是 H3 的已知不可控行为（详见 `analysis/字幕取证.md`）。",
        "> 本报告只做**发现**，不做修改；要不要处理由使用者决定。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("体检报告已写出：%s", path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="分段画面烧录字幕体检")
    parser.add_argument("--project", help="项目名（体检它的全部分段）")
    parser.add_argument("--id", help="只体检这些片段，多个用逗号分隔：seg-04,seg-05")
    parser.add_argument("--file", help="直接指定一个视频文件（与 --project 二选一）")
    parser.add_argument("--fps", type=float, default=4.0, help="采样频率，默认每秒 4 次")
    parser.add_argument("--gray", type=int, default=225, help="白字灰度阈值，默认 225")
    parser.add_argument("--band-floor", type=float, default=1.0,
                        help="下部带亮像素占比的候选阈值，默认 1.0")
    parser.add_argument("--slice-floor", type=float, default=5.0,
                        help="单条切片判定为文字的阈值，默认 5.0")
    parser.add_argument("--min-slices", type=int, default=3,
                        help="中间至少几条切片同时超标才算一行字，默认 3")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--write-report", action="store_true",
                        help="把结论写进 projects/<项目>/analysis/字幕体检-<时间>.md")
    args = parser.parse_args()

    if not args.project and not args.file:
        parser.error("至少要给 --project 或 --file")

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    ffmpeg = settings.path("ffmpeg.ffmpeg")
    ffprobe = settings.path("ffmpeg.ffprobe")

    targets = collect_targets(args, settings, log)
    if not targets:
        return 2

    print("=" * 78)
    print(f"字幕体检　文件数 {len(targets)}　时间 {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 78)
    results = []
    for path in targets:
        try:
            result = textcheck.check_video(
                ffmpeg, ffprobe, path,
                fps=args.fps, gray_threshold=args.gray, band_floor=args.band_floor,
                slice_floor=args.slice_floor, min_slices=args.min_slices,
            )
        except textcheck.TextCheckError as exc:
            log.error("%s 体检失败：%s", path.name, exc)
            return 2
        results.append(result)
        if not args.json:
            print(format_result(result))
            print()

    captions = [r for r in results if r.verdict == "caption"]
    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        print("-" * 78)
        if captions:
            print(f"汇总：{len(results)} 段里有 {len(captions)} 段发现字幕 —— "
                  f"{'、'.join(r.path.name for r in captions)}")
        else:
            print(f"汇总：{len(results)} 段全部干净，没有发现烧录字幕。")
        print("说明：字幕是模型的已知不可控行为，本工具只负责发现；"
              "要不要处理由使用者决定（备用方案见 analysis/字幕取证.md 第七节）。")

    if args.write_report:
        project_name = args.project or Path(targets[0]).parent.parent.name
        write_report(settings, project_name, results, log)
    return 1 if captions else 0


if __name__ == "__main__":
    raise SystemExit(main())
