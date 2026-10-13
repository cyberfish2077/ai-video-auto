"""给一个制作项目出一份中文报告（阶段八收尾工具，不占用显卡）。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe scripts\\project_report.py --project long120-01

默认写到 ``projects/<项目名>/report.md``（旧报告自动归档到 ``analysis/``）；
报告包含：概览、规格、分段明细、字幕体检、素材、合成记录、异常与事件、产物清单、下一步。

    # 出报告时跳过字幕体检（更快）
    .venv\\Scripts\\python.exe scripts\\project_report.py --project long120-01 --no-deep

    # 输出到别处 / 只打印不落盘
    .venv\\Scripts\\python.exe scripts\\project_report.py --project long120-01 --out D:\\tmp\\r.md
    .venv\\Scripts\\python.exe scripts\\project_report.py --project long120-01 --print
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

from video_auto import compose, config as cfg, logging_util, tasks, textcheck  # noqa: E402

LOG_NAME = "report"
#: 关注度较高、要在报告里单独点名的历史事件
NOTABLE_EVENTS = {
    "regen_for_subtitle_fix": "因字幕重跑",
    "regen_try3_no_text_words_prompt": "因字幕重跑（换写法）",
    "postprocess_delogo_repair": "做过定点像素修复",
    "revert_to_direct_generation": "回退为直接生成版",
    "reuse": "复用已有结果",
    "resume_adopt": "重启后收编已完成任务",
    "reset": "人工重置重跑",
}


def fmt_seconds(value) -> str:
    """把秒数写成好看的中文（None → 占位符）。"""
    if not value:
        return "—"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)} 分 {rest:.0f} 秒"


def collect(settings, project_name: str, *, deep: bool, log) -> dict:
    """把报告需要的素材全部读出来（只读，不改任何东西）。"""
    project_dir = settings.path("paths.projects") / project_name
    store = tasks.ProjectStore(project_dir)
    if not store.exists():
        raise FileNotFoundError(
            f"项目不存在：{store.path}\n解决办法：确认项目名，或先跑 make_script.py 建立项目。"
        )
    project = store.load()
    ffprobe = settings.path("ffmpeg.ffprobe")

    segments = []
    for seg in project.segments:
        item = {
            "id": seg.id, "status": seg.status, "attempts": seg.attempts,
            "elapsed": seg.elapsed_seconds, "output": seg.output_path,
            "error": seg.error, "chain_prev": seg.chain_prev,
            "events": [h.get("event") for h in (seg.history or [])],
        }
        path = Path(seg.output_path) if seg.output_path else None
        if path and path.exists():
            try:
                info = compose.probe_segment(ffprobe, path)
                item.update(size_bytes=path.stat().st_size, width=info.width,
                            height=info.height, duration=info.duration,
                            video_codec=info.video_codec, audio_codec=info.audio_codec,
                            fps=info.fps)
            except compose.ComposeError as exc:
                item["probe_error"] = str(exc).splitlines()[0]
        segments.append(item)

    text_checks = {}
    if deep:
        for item in segments:
            if item["status"] not in (tasks.STATUS_DONE, tasks.STATUS_REUSED):
                continue
            path = Path(item["output"] or "")
            if not path.exists():
                continue
            try:
                result = textcheck.check_video(settings.path("ffmpeg.ffmpeg"), ffprobe, path)
                text_checks[item["id"]] = result
            except textcheck.TextCheckError as exc:
                log.warning("%s 字幕体检失败：%s", item["id"], exc)

    compose_records = []
    for record_file in sorted((project_dir / "compose").glob("*.json")):
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        record["_file"] = record_file.name
        compose_records.append(record)

    materials_file = project_dir / "materials.json"
    material_count = None
    if materials_file.exists():
        try:
            material_count = len(json.loads(materials_file.read_text(encoding="utf-8"))
                                   .get("segments", []))
        except json.JSONDecodeError:
            material_count = None

    finals = sorted(p for p in (project_dir / "final").glob("*.mp4"))
    superseded = sorted(p for p in (project_dir / "final" / "_superseded").glob("*.mp4"))
    return {
        "project": project, "project_dir": project_dir, "segments": segments,
        "text_checks": text_checks, "compose_records": compose_records,
        "material_count": material_count, "finals": finals, "superseded": superseded,
    }


def render(data: dict, project_name: str) -> str:
    """把收集到的数据渲染成 Markdown 报告。"""
    project = data["project"]
    segments = data["segments"]
    counts: dict[str, int] = {}
    for item in segments:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    measured = [i for i in segments if i.get("duration")]
    total_elapsed = sum(float(i["elapsed"] or 0) for i in segments)
    generated = [i for i in segments if i["status"] == tasks.STATUS_DONE]
    captioned = [sid for sid, r in data["text_checks"].items() if r.verdict == "caption"]

    lines = [
        f"# 制作报告：{project_name}",
        "",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}　"
        f"工作流：`{project.target}`　"
        f"项目建立：{project.created_at or '—'}",
        "",
        "## 一、概览",
        "",
        f"* 分段：**{len(segments)} 段**，状态统计 `{counts}`",
        f"* 单段时长：{(project.defaults or {}).get('duration_seconds', '—')} 秒",
        f"* 生成耗时合计：**{total_elapsed / 60:.1f} 分钟**"
        f"（真正生成 {len(generated)} 段，平均 "
        f"{(total_elapsed / len(generated) / 60 if generated else 0):.1f} 分钟/段）",
        f"* 真实素材：{data['material_count'] if data['material_count'] is not None else '—'} 张参考图"
        f"（materials.json）",
    ]
    if measured:
        first = measured[0]
        lines.append(
            f"* 视频规格：{first.get('width')}×{first.get('height')}　"
            f"{first.get('video_codec')}/{first.get('audio_codec')}　"
            f"fps={first.get('fps')}"
        )
    if data["finals"]:
        for final in data["finals"]:
            lines.append(f"* 成片：`{final.name}`　"
                         f"{final.stat().st_size / 1024 ** 2:.2f} MB")
    lines += ["", "## 二、分段明细", "",
              "| 分段 | 状态 | 尝试 | 生成耗时 | 产物 | 大小 | 字幕体检 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in segments:
        size = f"{item['size_bytes'] / 1024 ** 2:.2f} MB" if item.get("size_bytes") else "—"
        name = Path(item["output"]).name if item["output"] else "—"
        check = "—"
        result = data["text_checks"].get(item["id"])
        if result is not None:
            spans = textcheck.caption_windows_in_seconds(result)
            check = f"**发现字幕** {'、'.join(spans)}" if spans else "干净"
        lines.append(
            f"| {item['id']} | {item['status']} | {item['attempts']} | "
            f"{fmt_seconds(item['elapsed'])} | {name} | {size} | {check} |"
        )

    lines += ["", "## 三、字幕体检结论", ""]
    if not data["text_checks"]:
        lines.append("（本次未做体检：用了 `--no-deep`，或没有已完成的分段。）")
    elif captioned:
        lines.append(f"**{len(captioned)} 段画面里烧进了字幕**：{'、'.join(captioned)}。")
        lines.append("")
        lines.append("这是 H3 的已知不可控行为（换种子/改写法都复现过，见 "
                     "`analysis/字幕取证.md`）。当前策略是**接受**；"
                     "确实需要无字幕版时，用该文档第七节的 `delogo` 定点修复参数，"
                     "或 `scripts/check_segment_text.py` 给出的时间段。")
    else:
        lines.append("全部干净，没有发现烧录字幕。")
    lines.append("")
    lines.append("> 复检命令：`.venv\\Scripts\\python.exe scripts\\check_segment_text.py "
                 f"--project {project_name} --write-report`")

    lines += ["", "## 四、合成记录", ""]
    if data["compose_records"]:
        lines += ["| 记录文件 | 输出 | 时长 | 大小 | 音频淡化 |", "| --- | --- | --- | --- | --- |"]
        for record in data["compose_records"]:
            name = Path(str(record.get("output", ""))).name
            size = record.get("size_bytes")
            size_text = f"{size / 1024 ** 2:.2f} MB" if isinstance(size, (int, float)) else "—"
            lines.append(
                f"| {record.get('_file', '—')} | {name} | "
                f"{record.get('duration', '—')} 秒 | "
                f"{size_text} | "
                f"{record.get('audio_fade', '—')} 秒 |"
            )
        lines.append("")
        lines.append("> 注意：距今记录里**没有 `dissolve`（交叉溶解）字段**，"
                     "成片时长对不上时只能反推；建议后续补记（见 `references/实测经验与坑.md`）。")
    else:
        lines.append("（还没有合成记录。）")

    lines += ["", "## 五、异常与关键事件", ""]
    problems = [i for i in segments if i["error"]]
    if problems:
        for item in problems:
            lines.append(f"* **{item['id']} 出错**：{str(item['error']).splitlines()[0]}")
    else:
        lines.append("* 没有失败的分段。")
    for item in segments:
        notable = [NOTABLE_EVENTS[e] for e in item["events"] if e in NOTABLE_EVENTS]
        if notable:
            unique = []
            for label in notable:
                if label not in unique:
                    unique.append(label)
            lines.append(f"* {item['id']}：{'、'.join(unique)}")
    if data["superseded"]:
        lines.append("* 被替换的旧成片（都在 `final/_superseded/`）："
                     + "、".join(p.name for p in data["superseded"]))

    lines += ["", "## 六、产物清单", ""]
    lines.append(f"* 项目目录：`{data['project_dir']}`")
    for item in segments:
        if item["output"]:
            lines.append(f"* {item['id']}：`{item['output']}`")

    lines += ["", "## 七、下一步建议", ""]
    if counts.get("pending") or counts.get("failed") or counts.get("running"):
        lines.append("* 还有分段没跑完：`scripts\\run_batch.py run --project "
                     f"{project_name}`（会自动续跑，不重复花钱）。")
    if captioned:
        lines.append("* 若要交付无字幕版：按 `analysis/字幕取证.md` 第七节做定点修复，"
                     "再把台账 `output_path` 指向修复版并重新合成。")
    if not data["finals"]:
        lines.append("* 还没合成：`scripts\\compose_final.py --project "
                     f"{project_name} --dissolve 0.6 --fade 0.25`。")
    lines.append("* 全流程一条命令：`scripts\\make_video.py --name <项目> "
                 "--brief-file <讲稿.txt> --minutes <分钟数>`。")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="出制作项目的中文报告")
    parser.add_argument("--project", required=True, help="项目名")
    parser.add_argument("--out", help="输出路径（默认 projects/<项目>/report.md）")
    parser.add_argument("--no-deep", action="store_true", help="跳过字幕体检（更快）")
    parser.add_argument("--print", dest="to_stdout", action="store_true",
                        help="只打印，不写文件")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    try:
        data = collect(settings, args.project, deep=not args.no_deep, log=log)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2

    text = render(data, args.project)
    if args.to_stdout:
        print(text)
        return 0

    target = Path(args.out) if args.out else data["project_dir"] / "report.md"
    if target.exists():
        archive_dir = data["project_dir"] / "analysis"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived = archive_dir / f"报告-{target.stat().st_mtime_ns}.md"
        target.replace(archived)
        log.info("旧报告已归档：%s", archived)
    target.write_text(text, encoding="utf-8")
    print(text)
    print("-" * 78)
    log.info("报告已写出：%s", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
