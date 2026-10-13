"""阶段八 第一步：把主题/讲稿变成一份可执行的生产目录。

    .venv\\Scripts\\python.exe scripts\\make_script.py --name long120-01 ^
        --brief-file data/briefs/long120.txt --minutes 2 --characters 2

产出（全部在 projects/<项目名>/ 下）：script.md、prompts/seg-XX.txt、
project.json（出片任务）、materials.json（素材生成任务）。

设计要点：DeepSeek 只写**内容**（剧情/镜头/台词/音效/分镜提示词），
格式（H3 中文时间轴模板 + 禁字幕条款 + 跨段连续性）由代码组装，
所以格式一定合规；校验不过就带着具体问题重问一次，最多 3 次。
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

from video_auto import config as cfg, deepseek, logging_util, script_gen  # noqa: E402

LOG_NAME = "make_script"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成剧本与生产目录（阶段八第一步）")
    parser.add_argument("--name", required=True, help="项目名（生产目录名）")
    parser.add_argument("--brief-file", required=True, help="主题/讲稿文本文件")
    parser.add_argument("--minutes", type=float, default=2.0, help="成片总时长（分钟）")
    parser.add_argument("--segment-seconds", type=float, default=15.0, help="单段时长（秒）")
    parser.add_argument("--aspect", default="16:9")
    parser.add_argument("--width", type=int, default=1376)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--characters", type=int, default=2, help="主要角色数量")
    parser.add_argument("--seed", type=int, default=20261012, help="种子基准值")
    parser.add_argument("--style", help="风格档位名（见 config/styles.yaml），不填用其中 default_profile")
    parser.add_argument("--force", action="store_true", help="覆盖已有生产目录")
    parser.add_argument("--dry-run", action="store_true", help="只问 DeepSeek 并校验，不落盘")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    style_name, style = cfg.load_style(args.style)

    brief_file = Path(args.brief_file)
    if not brief_file.is_absolute():
        brief_file = PROJECT_ROOT / brief_file
    if not brief_file.exists():
        log.error("主题/讲稿文件不存在：%s", brief_file)
        return 2
    brief = brief_file.read_text(encoding="utf-8").strip()
    if len(brief) < 10:
        log.error("主题/讲稿太短（少于 10 字），无法扩写：%s", brief_file)
        return 2

    total_seconds = args.minutes * 60
    expected = max(1, round(total_seconds / args.segment_seconds))
    project_dir = settings.path("paths.projects") / args.name
    if project_dir.exists() and any(project_dir.iterdir()) and not args.force:
        log.error("生产目录已存在且非空：%s（要覆盖请加 --force）", project_dir)
        return 2

    print("=" * 74)
    print(f"阶段八 第一步：生成剧本   项目：{args.name}   时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 74)
    print(f"讲稿：{brief_file}（{len(brief)} 字）")
    print(f"目标：{expected} 段 × {args.segment_seconds:.0f} 秒 = {total_seconds:.0f} 秒，"
          f"{args.aspect}，角色 {args.characters} 个")
    print(f"风格：{style_name}")

    client = deepseek.DeepSeekClient(settings)
    log.info("使用模型：%s", client.model)

    story = None
    messages = script_gen.build_story_messages(
        brief, total_seconds=total_seconds, segment_seconds=args.segment_seconds,
        aspect=args.aspect, character_count=args.characters,
        style_hint=style["story_style"],
    )
    for attempt in range(1, 4):
        log.info("向 DeepSeek 请求剧本（第 %d 次）", attempt)
        try:
            raw = client.chat(messages, json_mode=True, temperature=0.8)
        except deepseek.DeepSeekError as exc:
            log.error("调用 DeepSeek 失败：%s", exc)
            return 1
        try:
            story = script_gen.parse_story(
                raw, expected_segments=expected, segment_seconds=args.segment_seconds
            )
            break
        except script_gen.ScriptError as exc:
            log.warning("剧本校验未通过：%s", exc)
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    f"上面的 JSON 不合格，请修正后重新输出完整 JSON。问题：{exc}\n"
                    f"注意：segments 必须正好 {expected} 个，每段 3 个镜头且时间首尾相接铺满 "
                    f"0-{args.segment_seconds:.0f} 秒。"},
            ]

    if story is None:
        log.error("连续 3 次都没能生成合格剧本，已停止（未占用显卡）")
        return 1

    log.info("剧本通过校验：标题《%s》，角色 %d 个，分段 %d 段",
             story.title, len(story.characters), len(story.segments))
    for i, seg in enumerate(story.segments, start=1):
        log.info("  第 %d 段：%s", i, seg.summary)

    if args.dry_run:
        print("\n（dry-run：未写入文件）")
        return 0

    written = script_gen.write_production(
        project_dir, story,
        segment_seconds=args.segment_seconds, aspect=args.aspect,
        width=args.width, height=args.height, style_prefix=style["style_prefix"],
        base_seed=args.seed, brief=brief,
        bgm_line=style["bgm_line"], negative=style["negative"],
        material_style=style["material_style"],
    )
    print()
    for key, path in written.items():
        log.info("已写出 %-10s %s", key, path)

    # 落一份本次生成纪要，便于回溯
    archive = project_dir / "generation.json"
    archive.write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": client.model,
        "brief_file": str(brief_file),
        "total_seconds": total_seconds,
        "segment_seconds": args.segment_seconds,
        "aspect": args.aspect,
        "characters": [c.name for c in story.characters],
        "title": story.title,
        "logline": story.logline,
        "style": style_name,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已写出 generation.json（生成纪要）")
    print()
    log.info("下一步：run_batch.py create --from-json projects/%s/materials.json", args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
