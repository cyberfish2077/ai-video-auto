"""《剪枝者》全量生产驱动（阶段十）。

把已有脚本按固定顺序串起来跑，每一步的输出同时写到控制台和
``data/logs/jianzhizhe-full-<日期>.log``，任何一步失败就停下并打印该步的日志尾部
与"怎么续跑"。整条链是可断点续跑的：重跑本脚本会跳过已完成的部分（台账 + 指纹去重）。

顺序（有依赖，别打乱）::

    素材补齐 → 分镜图（多图参考）→ 同步素材 → 配音 → 视频长跑 → 合成 → 体检 → 报告

用法（在项目根目录）::

    .venv\\Scripts\\python.exe scripts\\run_jianzhizhe_full.py
    .venv\\Scripts\\python.exe scripts\\run_jianzhizhe_full.py --only 视频长跑   # 只跑其中一步
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
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

PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
SCRIPTS = PROJECT_ROOT / "scripts"
PROJECT = "jianzhizhe"

#: 本脚本自己拉起过 ComfyUI 时记在这（跑完要负责关掉，别长期占显存）
MARKER: dict[str, int] = {"started_pid": 0}

#: ComfyUI 启动参数（与 references/安装与前置条件.md 第 4.2 节逐字一致）
COMFY_ARGS = ["-s", "ComfyUI\\main.py", "--windows-standalone-build",
              "--listen", "0.0.0.0", "--enable-cors-header", "*", "--use-sage-attention"]

#: 不带 SageAttention 的启动参数（对照实验用，见 --no-sage 说明）
COMFY_ARGS_NO_SAGE = ["-s", "ComfyUI\\main.py", "--windows-standalone-build",
                      "--listen", "0.0.0.0", "--enable-cors-header", "*"]


def progress_count(settings, project_names: list[str]) -> int:
    """统计这些台账里已完成/已复用的片段总数，用来判断"重试有没有真的推进"。"""
    total = 0
    for name in project_names:
        path = settings.path("paths.projects") / name / "segments.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        total += sum(1 for seg in (data.get("segments") or [])
                     if seg.get("status") in ("done", "reused"))
    return total


def comfy_settings():
    """读取 config/settings.yaml 里的 ComfyUI 地址与安装目录（不猜、不写死路径）。"""
    from video_auto import config as cfg
    settings = cfg.load_settings()
    return settings.get("comfyui.base_url"), settings.path("comfyui.install_dir")


def comfy_online(base_url: str, timeout: float = 5.0) -> bool:
    """ComfyUI 是否在线（只读探测，不提交任何任务）。"""
    try:
        with urllib.request.urlopen(f"{base_url}/system_stats", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_comfy(install_dir: Path, log_file: Path, *, no_sage: bool = False,
                wait_seconds: int = 420) -> bool:
    """按文档里的命令重启 ComfyUI，并等它真正可用。

    背景：本机 ComfyUI 有"跑完一段长任务后自行退出"的历史现象（日志里不报错），
    无人值守长跑必须能自动把它拉起来，否则整批会卡死在那一步。
    """
    exe = install_dir / "python_embeded" / "python.exe"
    if not exe.exists():
        print(f"[错误] 找不到 ComfyUI 解释器：{exe}")
        return False
    comfy_args = COMFY_ARGS_NO_SAGE if no_sage else COMFY_ARGS
    print(f"[{datetime.now():%H:%M:%S}] 启动 ComfyUI：{exe.name} {comfy_args[1]}"
          + ("（不带 SageAttention）" if no_sage else "（带 SageAttention）"))
    out = (log_file.parent / "comfyui-autostart.log").open("a", encoding="utf-8")
    out.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 ComfyUI"
              f"{'（no-sage）' if no_sage else ''} ===\n")
    out.flush()
    creation = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    started = subprocess.Popen([str(exe), *comfy_args], cwd=str(install_dir),
                               stdout=out, stderr=subprocess.STDOUT, creationflags=creation)
    MARKER["started_pid"] = started.pid
    base_url, _ = comfy_settings()
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(10)
        if comfy_online(base_url):
            print(f"[{datetime.now():%H:%M:%S}] ComfyUI 已就绪")
            return True
    print(f"[错误] 等了 {wait_seconds} 秒 ComfyUI 仍未就绪，请查看 "
          f"{log_file.parent / 'comfyui-autostart.log'}")
    return False


def steps() -> list[tuple[str, list[str]]]:
    """返回 (步骤名, 命令参数) 列表；命令一律用项目虚拟环境的解释器。"""
    py = str(PYTHON)
    return [
        ("素材补齐", [py, str(SCRIPTS / "run_batch.py"), "run",
                      "--project", f"{PROJECT}-materials"]),
        ("分镜图", [py, str(SCRIPTS / "run_batch.py"), "run",
                    "--project", f"{PROJECT}-boards"]),
        # 不带 --allow-missing：这一步顺便当"素材齐不齐"的把关
        ("同步素材", [py, str(SCRIPTS / "sync_inputs.py"), "--project", PROJECT]),
        ("建配音台账", [py, str(SCRIPTS / "run_batch.py"), "create",
                        "--from-json", f"projects/{PROJECT}/narration.json", "--force"]),
        ("配音", [py, str(SCRIPTS / "run_batch.py"), "run",
                  "--project", f"{PROJECT}-narration"]),
        ("视频长跑", [py, str(SCRIPTS / "run_batch.py"), "run", "--project", PROJECT]),
        ("合成粗剪", [py, str(SCRIPTS / "compose_final.py"), "--project", PROJECT]),
        ("字幕体检", [py, str(SCRIPTS / "check_segment_text.py"), "--project", PROJECT]),
        ("制作报告", [py, str(SCRIPTS / "project_report.py"), "--project", PROJECT]),
    ]


def ledger_names_for(step_name: str) -> list[str]:
    """这一步会写哪个台账（用来判断重试有没有真的推进）。"""
    return {
        "素材补齐": [f"{PROJECT}-materials"],
        "分镜图": [f"{PROJECT}-boards"],
        "配音": [f"{PROJECT}-narration"],
        "视频长跑": [PROJECT],
    }.get(step_name, [])


#: 允许返回非零的步骤：它们的非零返回码是"发现问题"而不是"执行失败"。
#: 例：字幕体检发现烧录字幕会返回 1 —— 那是结论，不该让整条流水线中止。
INFORMATIONAL_STEPS = {"字幕体检"}


def run_step(name: str, cmd: list[str], log_file: Path, log) -> int:
    """执行一个步骤，输出同时打到控制台与日志文件。"""
    print()
    print("=" * 78)
    print(f"[{datetime.now():%H:%M:%S}] 步骤：{name}")
    print("=" * 78)
    log.write(f"\n{'=' * 78}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 步骤：{name}\n{'=' * 78}\n")
    log.flush()
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()
    code = proc.wait()
    print(f"[{datetime.now():%H:%M:%S}] 步骤「{name}」结束，返回码 {code}")
    log.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 步骤「{name}」结束，返回码 {code}\n")
    log.flush()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="《剪枝者》全量生产驱动")
    parser.add_argument("--only", help="只跑名字匹配这个步骤（子串匹配）")
    parser.add_argument("--stop-after", help="跑到这个步骤（含）就停")
    parser.add_argument("--retries", type=int, default=60,
                        help="每步失败后的重试次数上限（默认 60）。"
                             "ComfyUI 的 SageAttention 有间歇性原生崩溃（实测每 1~2 个任务可能死一次），"
                             "所以预算给得比较宽；真正防死循环靠 --stall-limit")
    parser.add_argument("--stall-limit", type=int, default=3,
                        help="连续几次重试都没有任何新产物就放弃该步（默认 3）")
    parser.add_argument("--no-comfy-autostart", action="store_true",
                        help="不自动重启 ComfyUI（默认会检查并在掉线时按文档命令拉起）")
    parser.add_argument("--no-sage", action="store_true",
                        help="重启 ComfyUI 时不带 --use-sage-attention（默认**带**，与文档命令一致）。"
                             "2026-10-14 起出图工作流已显式指定非 sage 后端"
                             "（scripts/add_attention_backend.py 插入 ModelAttentionBackend），"
                             "所以带 sage 也不会再崩；这个开关只作为应急退路保留")
    parser.add_argument("--keep-comfy", action="store_true",
                        help="跑完不关闭 ComfyUI（默认跑完自动关闭，释放显存；"
                             "只关这个脚本自己拉起来的那一个）")
    args = parser.parse_args()

    logs_dir = PROJECT_ROOT / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{PROJECT}-full-{datetime.now():%Y%m%d-%H%M}.log"

    plan = steps()
    if args.only:
        plan = [item for item in plan if args.only in item[0]]
        if not plan:
            print(f"[错误] 没有名字包含「{args.only}」的步骤", file=sys.stderr)
            return 2

    print(f"《剪枝者》全量生产：{len(plan)} 个步骤，日志 {log_path}")
    started = datetime.now()
    from video_auto import config as cfg
    settings = cfg.load_settings()
    base_url = str(settings.get("comfyui.base_url"))
    install_dir = settings.path("comfyui.install_dir")
    with log_path.open("w", encoding="utf-8") as log:
        for name, cmd in plan:
            log.write("命令：" + " ".join(cmd) + "\n")
        log.flush()
        for name, cmd in plan:
            code = 1
            stalls = 0
            for attempt in range(1, args.retries + 1):
                if not args.no_comfy_autostart:
                    if not comfy_online(base_url):
                        print(f"[{datetime.now():%H:%M:%S}] ComfyUI 不在线"
                              f"（第 {attempt} 次尝试前检查）")
                        if not start_comfy(install_dir, log_path, no_sage=args.no_sage):
                            code = 3
                            break
                before = progress_count(settings, ledger_names_for(name))
                code = run_step(name, cmd, log_path, log)
                if code != 0 and name in INFORMATIONAL_STEPS:
                    print(f"[提示] 「{name}」返回 {code} —— 这是它的结论（发现了问题），"
                          f"不算执行失败，继续下一步。")
                    log.write(f"[提示] 「{name}」返回 {code}，按信息性步骤处理，继续。\n")
                    log.flush()
                    code = 0
                if code == 0:
                    break
                if attempt < args.retries:
                    after = progress_count(settings, ledger_names_for(name))
                    if after > before:
                        stalls = 0
                        print(f"[重试] 「{name}」第 {attempt} 次中断，但已推进 {after - before} 个片段"
                              f"（累计 {after}），继续")
                    else:
                        stalls += 1
                        print(f"[重试] 「{name}」第 {attempt} 次失败且没有新产物"
                              f"（连续 {stalls}/{args.stall_limit} 次）")
                        if stalls >= args.stall_limit:
                            print(f"[中止] 「{name}」连续 {stalls} 次没有任何进展，"
                                  f"像是卡在同一个问题上，交给人来看。")
                            code = 4
                            break
                    time.sleep(60)
            if code != 0:
                print()
                print(f"[中止] 步骤「{name}」连续 {args.retries} 次失败（返回码 {code}）。")
                print(f"       日志：{log_path}")
                print(f"       续跑：.venv\\Scripts\\python.exe {Path(__file__).name} --only {name}")
                # 中止也别把 ComfyUI 丢在那儿占显存；续跑时脚本会自动再拉起来
                stop_comfy_if_we_started(args, log_path)
                return code
            if args.stop_after and name == args.stop_after:
                print(f"[停止] 已按要求停在「{name}」之后。")
                break

    elapsed = (datetime.now() - started).total_seconds() / 60
    print()
    print(f"全部步骤完成，用时 {elapsed:.1f} 分钟。日志：{log_path}")
    stop_comfy_if_we_started(args, log_path)
    return 0


def stop_comfy_if_we_started(args, log_path: Path) -> None:
    """跑完关掉 ComfyUI，别长期占显存（用户 2026-10-14 的要求）。

    只关本脚本自己拉起来的那一个；如果 ComfyUI 原本就是用户自己开的，就不动它。
    """
    if args.keep_comfy or not MARKER["started_pid"]:
        if MARKER["started_pid"] and args.keep_comfy:
            print("[提示] --keep-comfy：保留 ComfyUI 不关闭。")
        return
    print(f"[{datetime.now():%H:%M:%S}] 关闭本次拉起的 ComfyUI（PID {MARKER['started_pid']}），释放显存")
    subprocess.run(["taskkill", "/PID", str(MARKER["started_pid"]), "/T", "/F"],
                   capture_output=True, text=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 已关闭 ComfyUI PID {MARKER['started_pid']}\n")


if __name__ == "__main__":
    raise SystemExit(main())
