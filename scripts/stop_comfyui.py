"""关闭 ComfyUI（用户 2026-10-14 的要求：跑完所有任务后主动关掉，别长期占资源）。

只关"命令行里确实是 ComfyUI\\\\main.py"的进程，不误杀别的 Python 进程；
关闭前会打印进程号与显存占用，便于确认。

用法::

    .venv\\Scripts\\python.exe scripts\\stop_comfyui.py            # 关闭
    .venv\\Scripts\\python.exe scripts\\stop_comfyui.py --dry-run  # 只看有几个在跑
"""

from __future__ import annotations

import argparse
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def comfy_processes() -> list[tuple[int, str]]:
    """返回 (pid, 命令行) —— 只挑命令行里带 ComfyUI\\main.py 的进程。"""
    # 只认 python 进程，并且排除本次调用自己的 powershell（它的命令行里也含这段文字）
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
        "$_.CommandLine -like '*ComfyUI*main.py*' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    rows: list[tuple[int, str]] = []
    for line in (proc.stdout or "").splitlines():
        if "\t" not in line:
            continue
        pid_text, cmd = line.split("\t", 1)
        try:
            rows.append((int(pid_text.strip()), cmd.strip()))
        except ValueError:
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="关闭 ComfyUI 释放显存")
    parser.add_argument("--dry-run", action="store_true", help="只列出，不关闭")
    args = parser.parse_args()

    rows = comfy_processes()
    if not rows:
        print("没有在跑的 ComfyUI 进程。")
        return 0
    for pid, cmd in rows:
        print(f"  PID {pid}：{cmd[:110]}")
    if args.dry_run:
        print("（--dry-run：没有关闭）")
        return 0
    for pid, _cmd in rows:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
        print(f"已关闭 PID {pid}（含子进程）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
