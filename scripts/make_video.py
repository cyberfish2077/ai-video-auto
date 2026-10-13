"""一条命令做完整条长视频（阶段八收尾）：讲稿 → 成片。

它只是"串流程"，不重复实现业务逻辑：每一步都调用已有脚本（剧本 / 素材 / 分段 / 合成 /
体检 / 报告），并且**可反复执行**——已经做完的步骤会自动跳过，中途失败再跑一次就接着做。

用法（在项目根目录执行）::

    # 最常见：给一个讲稿文件，做一条 2 分钟视频
    .venv\\Scripts\\python.exe scripts\\make_video.py ^
        --name my-film --brief-file data\\briefs\\my-film.txt --minutes 2

    # 先看看会执行什么（不跑任何东西）
    .venv\\Scripts\\python.exe scripts\\make_video.py --name my-film ^
        --brief-file data\\briefs\\my-film.txt --plan

    # 只跑其中几步 / 跳过某几步
    .venv\\Scripts\\python.exe scripts\\make_video.py --name my-film ^
        --brief-file data\\briefs\\my-film.txt --steps segments,compose

步骤与产物：

========= ============================================ =================================
步骤       做什么                                        主要产物
========= ============================================ =================================
script     DeepSeek 写剧本 + 组装每段提示词              projects/<名>/{script.md,prompts/}
ledger     按 project.json 建分段任务台账                projects/<名>/segments.json
materials  Qwen-Image 出角色图与分镜图                   projects/<名>/ 与 <名>-materials/
sync       把素材同步进 ComfyUI 的 input 目录            ComfyUI\\input\\*.png
segments   H3 逐段出片（每段约 8.3 分钟）                projects/<名>/segments/*.mp4
compose    FFmpeg 合成成片                               projects/<名>/final/<名>.mp4
check      字幕体检（发现烧录字幕，只报告不改）            projects/<名>/analysis/字幕体检-*.md
report     出一份中文制作报告                            projects/<名>/report.md
========= ============================================ =================================

设计要点：

* **逐步规划、逐步执行**：每一步执行前才决定它的命令，所以后一步能看到前一步刚产出的文件；
* **在线检查前置**：需要 ComfyUI 的步骤（出图/出片）在开跑前统一检查一次，连不上就直接
  告诉你"先启动 ComfyUI"，不会白等；
* **不覆盖成片**：合成默认不覆盖已有成片，要覆盖得显式给 ``--force-compose``。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
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

from video_auto import comfy, config as cfg, logging_util  # noqa: E402

LOG_NAME = "make_video"
ALL_STEPS = ["script", "ledger", "materials", "sync", "segments", "compose", "check",
             "report"]
#: 需要 ComfyUI 在线的步骤
STEPS_NEEDING_COMFY = {"materials", "segments"}


def python_exe() -> str:
    """一律用当前虚拟环境的解释器（绝不落到系统 py 或 ComfyUI 的 python_embeded）。"""
    return sys.executable


def run_step(title: str, cmd: list[str], log) -> int:
    """起一个子进程跑一步，实时把它的输出打到控制台，返回退出码。"""
    print()
    print("─" * 78)
    print(f"▶ {title}")
    print(f"  {' '.join(cmd)}")
    print("─" * 78)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.time()
    process = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, text=True,
                               encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip())
    code = process.wait()
    log.info("步骤「%s」结束：退出码 %s，用时 %.1f 秒", title, code, time.time() - started)
    return code


def comfyui_status(settings) -> tuple[bool, str]:
    """检查 ComfyUI 是否在线（出图/出片都需要它）。"""
    try:
        client = comfy.ComfyClient(str(settings.get("comfyui.base_url")), timeout=8)
        running, pending = client.queue_state()
        return True, f"队列 运行中 {running} / 等待 {pending}"
    except Exception as exc:  # noqa: BLE001 —— 任何连不上都归到这里
        return False, str(exc)


def plan_step(step: str, ctx: dict, state: dict) -> list[tuple[str, list[str]]]:
    """决定某一步要执行什么命令。

    ``state`` 是"计划/执行到目前为止"的虚拟状态：``--plan`` 模式下前面步骤并不会真的产出
    文件，靠它才能把整条链路的命令正确排出来（例如 materials 依赖 script 产出的
    ``materials.json``）。返回 ``[(说明, 命令)]``，命令为空列表表示这一步跳过。
    """
    args = ctx["args"]
    py = ctx["py"]
    script_dir: Path = ctx["script_dir"]
    project_dir: Path = ctx["project_dir"]
    material_dir: Path = ctx["material_dir"]
    brief: Path | None = ctx["brief"]

    if step == "script":
        if (project_dir / "project.json").exists() or state.get("script"):
            return [("script：已有剧本，跳过", [])]
        if not brief or not brief.exists():
            raise FileNotFoundError(
                f"还没有剧本，需要 --brief-file 指定讲稿文件（当前值：{brief or '未提供'}）。\n"
                f"解决办法：把讲稿存成 txt 放进 data\\briefs\\，再用 --brief-file 指过去。"
            )
        cmd = [py, str(script_dir / "make_script.py"), "--name", args.name,
               "--brief-file", str(brief), "--minutes", str(args.minutes),
               "--segment-seconds", str(args.segment_seconds),
               "--characters", str(args.characters), "--aspect", args.aspect,
               "--width", str(args.width), "--height", str(args.height)]
        if args.style:
            cmd += ["--style", args.style]
        if args.seed is not None:
            cmd += ["--seed", str(args.seed)]
        state["script"] = True
        return [("script：DeepSeek 写剧本与提示词", cmd)]

    if step == "materials":
        material_json = project_dir / "materials.json"
        if not material_json.exists() and not state.get("script"):
            return [("materials：还没有 materials.json（要先跑 script），跳过", [])]
        steps: list[tuple[str, list[str]]] = []
        if not (material_dir / "segments.json").exists() and not state.get("materials_ledger"):
            steps.append(("materials：新建素材台账",
                          [py, str(script_dir / "run_batch.py"), "create",
                           "--from-json", str(material_json)]))
            state["materials_ledger"] = True
        steps.append(("materials：出角色图与分镜图（已完成的会自动跳过）",
                      [py, str(script_dir / "run_batch.py"), "run",
                       "--project", f"{args.name}-materials"]))
        return steps

    if step == "ledger":
        ledger = project_dir / "segments.json"
        project_json = project_dir / "project.json"
        if ledger.exists():
            return [("ledger：分段台账已存在，跳过", [])]
        if not project_json.exists() and not state.get("script"):
            return [("ledger：还没有 project.json（要先跑 script），跳过", [])]
        state["ledger"] = True
        return [("ledger：按 project.json 建分段台账",
                 [py, str(script_dir / "run_batch.py"), "create",
                  "--from-json", str(project_json)])]

    if step == "sync":
        return [("sync：素材同步进 ComfyUI 的 input 目录",
                 [py, str(script_dir / "sync_inputs.py"), "--project", args.name])]

    if step == "segments":
        return [("segments：H3 逐段出片（每段约 8.3 分钟）",
                 [py, str(script_dir / "run_batch.py"), "run", "--project", args.name])]

    if step == "compose":
        cmd = [py, str(script_dir / "compose_final.py"), "--project", args.name,
               "--dissolve", str(args.dissolve), "--fade", str(args.fade)]
        if args.bgm:
            bgm = Path(args.bgm)
            cmd += ["--bgm", str(bgm if bgm.is_absolute() else PROJECT_ROOT / bgm)]
        if args.force_compose:
            cmd.append("--force")
        return [("compose：合成成片", cmd)]

    if step == "check":
        return [("check：字幕体检（只报告，不修改画面）",
                 [py, str(script_dir / "check_segment_text.py"),
                  "--project", args.name, "--write-report"])]

    if step == "report":
        return [("report：出一份中文制作报告",
                 [py, str(script_dir / "project_report.py"), "--project", args.name])]

    raise ValueError(f"未知步骤：{step}")


def main() -> int:
    parser = argparse.ArgumentParser(description="一条命令做完整条长视频")
    parser.add_argument("--name", required=True, help="项目名（生产目录名）")
    parser.add_argument("--brief-file", help="讲稿/主题文本文件（首次生成剧本时必填）")
    parser.add_argument("--minutes", type=float, default=2.0, help="成片总时长（分钟），默认 2")
    parser.add_argument("--segment-seconds", type=float, default=15.0, help="单段秒数，默认 15")
    parser.add_argument("--characters", type=int, default=2, help="主要角色数量，默认 2")
    parser.add_argument("--style", help="风格档位名（见 config/styles.yaml），不填用其中 default_profile")
    parser.add_argument("--seed", type=int, help="种子基准值（不填由脚本决定）")
    parser.add_argument("--aspect", default="16:9 (Widescreen)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=736)
    parser.add_argument("--dissolve", type=float, default=0.6, help="分段间交叉溶解秒数，默认 0.6")
    parser.add_argument("--fade", type=float, default=0.25, help="音频淡化秒数，默认 0.25")
    parser.add_argument("--bgm", help="可选的背景音乐文件（mp3 等）")
    parser.add_argument("--steps", help="只跑这些步骤（逗号分隔），默认全跑")
    parser.add_argument("--skip", help="跳过这些步骤（逗号分隔）")
    parser.add_argument("--plan", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--force-compose", action="store_true",
                        help="允许覆盖已有成片（默认不覆盖，保护旧成片）")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    style_name, _ = cfg.load_style(args.style)  # 提前校验档位名，避免跑到一半才发现写错
    projects_root = settings.path("paths.projects")
    brief = Path(args.brief_file) if args.brief_file else None
    if brief and not brief.is_absolute():
        brief = PROJECT_ROOT / brief
    ctx = {
        "args": args, "py": python_exe(), "script_dir": PROJECT_ROOT / "scripts",
        "project_dir": projects_root / args.name,
        "material_dir": projects_root / f"{args.name}-materials", "brief": brief,
    }

    requested = [s.strip() for s in (args.steps or "").split(",") if s.strip()] or list(ALL_STEPS)
    skipped = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    unknown = set(requested) - set(ALL_STEPS)
    if unknown:
        log.error("--steps 里有不认识的步骤：%s（可选：%s）",
                  "、".join(sorted(unknown)), "、".join(ALL_STEPS))
        return 2
    steps = [s for s in requested if s not in skipped]

    print("=" * 78)
    print(f"一条命令做长视频　项目：{args.name}　时间 {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"目标时长：{args.minutes} 分钟　单段 {args.segment_seconds} 秒")
    print(f"风格档位：{style_name}　（可用档位见 config/styles.yaml）")
    print(f"步骤：{' → '.join(steps)}")
    print("=" * 78)

    state: dict = {}
    if args.plan:
        for step in steps:
            try:
                for title, cmd in plan_step(step, ctx, state):
                    print(f"  · {title}")
            except FileNotFoundError as exc:
                print(f"  · ⚠ {exc}")
        print()
        print("（--plan：只列计划，未执行任何步骤）")
        return 0

    if STEPS_NEEDING_COMFY & set(steps):
        online, detail = comfyui_status(settings)
        if not online:
            log.error("ComfyUI 连不上（%s）：%s\n"
                      "解决办法：先用启动器把 ComfyUI 开起来，再重新执行本命令；"
                      "已经做完的步骤会自动跳过，不会重复消耗显卡。",
                      settings.get("comfyui.base_url"), detail)
            return 2
        print(f"ComfyUI 在线（{detail}）")

    for step in steps:
        try:
            plans = plan_step(step, ctx, state)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            return 2
        for title, cmd in plans:
            if not cmd:
                print(f"\n（跳过：{title}）")
                continue
            code = run_step(title, cmd, log)
            if code == 0:
                continue
            if "check_segment_text.py" in " ".join(cmd) and code == 1:
                print("\n注意：体检发现有的分段烧进了字幕。这不影响流程继续 —— "
                      "详见项目目录 analysis/ 下的体检报告；要不要处理由你决定"
                      "（备用方案见 analysis/字幕取证.md 第七节）。")
                continue
            log.error("步骤「%s」失败（退出码 %s）。\n"
                      "解决办法：看上面报错的最后几行；修好后**重新执行一次本命令**即可，"
                      "已完成的部分会自动跳过。", title, code)
            return 1

    project_dir: Path = ctx["project_dir"]
    final = project_dir / "final" / f"{args.name}.mp4"
    print()
    print("=" * 78)
    print("本次要做的步骤全部完成。")
    if final.exists():
        print(f"成片：{final}（{final.stat().st_size / 1024 ** 2:.2f} MB）")
    print(f"报告：{project_dir / 'report.md'}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
