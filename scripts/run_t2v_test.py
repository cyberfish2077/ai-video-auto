"""T2V 最小链路试跑（阶段五 第二小步）。

流程：读工作流副本 → 改写参数（提示词/时长/分辨率/种子/输出名）→ 转 API 格式
→ 提交给 ComfyUI → 轮询到结束 → 取回文件 → 用 ffprobe 校验 → 落盘运行记录。

用法（在项目根目录执行）::

    # 只看转换结果，不提交（不占用显卡）
    .venv\\Scripts\\python.exe scripts\\run_t2v_test.py --dry-run

    # 真正提交并等待出片
    .venv\\Scripts\\python.exe scripts\\run_t2v_test.py --duration 5 --seed 1234

参数可覆盖：``--prompt-file`` / ``--prompt`` / ``--duration`` / ``--width`` / ``--height``
``--seed`` / ``--prefix`` / ``--target``。
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
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
# 实时刷新输出：任务要跑好几分钟，不能等结束才看到进度
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import comfy, config as cfg, workflow as wf_mod  # noqa: E402


def say(level: str, title: str, detail: str = "") -> None:
    print(f"[{level}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")


def probe_video(ffprobe: Path, path: Path) -> dict:
    """用 ffprobe 检查文件能否正常解码，返回时长/分辨率/编码信息。"""
    cmd = [
        str(ffprobe), "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,nb_frames",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 无法解码该文件：{(proc.stderr or '').strip()[:300]}")
    return json.loads(proc.stdout or "{}")


def main() -> int:
    parser = argparse.ArgumentParser(description="T2V 最小链路试跑（阶段五）")
    parser.add_argument("--target", default="t2v_4step", help="config/workflow_targets.yaml 里的工作流名")
    parser.add_argument("--prompt-file", default=str(PROJECT_ROOT / "data/projects/smoke-01/prompt.txt"))
    parser.add_argument("--prompt", help="直接给定提示词（优先于 --prompt-file）")
    parser.add_argument("--duration", type=float, help="目标时长（秒）")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--prefix", help="输出文件名前缀")
    parser.add_argument("--dry-run", action="store_true", help="只做转换与校验，不提交任务")
    parser.add_argument("--max-wait", type=int, help="最长等待秒数（默认取配置）")
    args = parser.parse_args()

    print("=" * 68)
    print("T2V 最小链路试跑（阶段五 第二小步）")
    print("=" * 68)

    settings = cfg.load_settings()
    base_url = str(settings.get("comfyui.base_url"))
    output_dir = settings.path("comfyui.output_dir")
    cache_file = settings.path("paths.cache") / "object_info_cache.json"
    targets = cfg.load_workflow_targets()

    if args.target not in targets:
        say("错误", f"配置里没有名为 {args.target} 的工作流", f"可选：{sorted(targets)}")
        return 2
    target = targets[args.target]
    workflow_path = settings.path("paths.workflows") / Path(str(target["file"])).name
    if not workflow_path.exists():
        say("错误", f"工作流副本不存在：{workflow_path}", "解决办法：从 ComfyUI 目录复制到 workflows/ 后再试。")
        return 2

    # ---------------- 1. 准备参数 ----------------
    if args.prompt:
        prompt_text = args.prompt
        prompt_source = "命令行 --prompt"
    else:
        prompt_file = Path(args.prompt_file)
        if not prompt_file.exists():
            say("错误", f"提示词文件不存在：{prompt_file}")
            return 2
        prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        prompt_source = str(prompt_file)
    if not prompt_text:
        say("错误", "提示词为空", "解决办法：在提示词文件里写入内容，或用 --prompt 直接给定。")
        return 2

    overrides_cfg: dict = target.get("overrides", {})
    values: dict[str, object] = {}
    if "prompt" in overrides_cfg:
        values["prompt"] = prompt_text
    if "duration_seconds" in overrides_cfg:
        values["duration_seconds"] = args.duration if args.duration else 5.0
    if "width" in overrides_cfg:
        values["width"] = args.width or 960
    if "height" in overrides_cfg:
        values["height"] = args.height or 544
    if "seed" in overrides_cfg:
        values["seed"] = args.seed if args.seed is not None else random.randint(1, 2**31 - 1)
    if "output_prefix" in overrides_cfg:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        values["output_prefix"] = args.prefix or f"video_auto/smoke-{stamp}"

    # 支持 same_as：把某个改写点设成与另一个改写点相同的值（用于双保险）
    for key, spec in overrides_cfg.items():
        if isinstance(spec, dict) and spec.get("same_as"):
            source_key = str(spec["same_as"])
            if source_key in values:
                values[key] = values[source_key]

    overrides: dict[int, dict[str, object]] = {}
    for key, value in values.items():
        spec = overrides_cfg.get(key)
        if not spec:
            continue
        overrides.setdefault(int(spec["node"]), {})[str(spec["input"])] = value

    say("OK", f"使用工作流：{target.get('label', args.target)}", str(workflow_path))
    print(f"        提示词来源：{prompt_source}（{len(prompt_text)} 字）")
    for key, value in values.items():
        shown = str(value)
        if key == "prompt":
            shown = shown.replace("\n", " ⏎ ")[:90] + "…"
        print(f"        {key} = {shown}")

    # ---------------- 2. 转换成 API 格式 ----------------
    object_info = comfy.load_object_info(base_url, cache_file)
    ui = wf_mod.load_ui_workflow(workflow_path, object_info)
    try:
        api_prompt = ui.to_api_prompt(overrides)
    except wf_mod.ConvertError as exc:
        say("错误", "工作流转换失败", str(exc))
        return 1

    say("OK", f"已转换为 API 格式：{len(api_prompt)} 个节点",
        f"（源工作流共 {len(ui.nodes)} 个节点，其余为界面节点或已跳过节点）")
    ignored = ui.ignored_node_types()
    if ignored:
        say("提示", "以下节点本机没有装对应的插件，但它们没有连线、不影响运行，已自动忽略",
            "\n".join(f"{t}：节点 {ids}" for t, ids in sorted(ignored.items())))

    # 关键节点核对：确认提示词与时长确实写进去了
    checks = [
        ("提示词节点", overrides_cfg.get("prompt"), prompt_text[:40]),
        ("时长节点", overrides_cfg.get("duration_seconds"), values.get("duration_seconds")),
        ("输出前缀", overrides_cfg.get("output_prefix"), values.get("output_prefix")),
    ]
    for label, spec, expect in checks:
        if not spec:
            continue
        node_key, input_name = str(spec["node"]), str(spec["input"])
        actual = (api_prompt.get(node_key) or {}).get("inputs", {}).get(input_name)
        ok = str(actual).startswith(str(expect)[:30])
        say("OK" if ok else "错误", f"{label}（{node_key}.{input_name}）",
            f"实际写入：{str(actual)[:60]}")
        if not ok:
            return 1

    if args.dry_run:
        dump = settings.path("paths.cache") / "last_api_prompt.json"
        dump.write_text(json.dumps(api_prompt, ensure_ascii=False, indent=2), encoding="utf-8")
        say("OK", "dry-run 结束，未提交任务", f"转换结果已写入：{dump}")
        return 0

    # ---------------- 3. 提交 ----------------
    client = comfy.ComfyClient(base_url, timeout=60)
    running, pending = client.queue_state()
    if running or pending:
        say("警告", f"ComfyUI 队列非空闲（运行中 {running}，等待 {pending}）", "仍会提交，任务会排在后面。")

    started = time.time()
    prompt_id = client.submit(api_prompt, extra_data={"extra_pnginfo": {"workflow_name": args.target}})
    say("OK", "任务已提交", f"prompt_id = {prompt_id}")
    print("        提示：任务在后台运行，关掉这个窗口不会中断；用同样的 prompt_id 可以查询进度。")

    timeout_seconds = args.max_wait or int(settings.get("comfyui.job_timeout_seconds", 3600))
    ticks: list[tuple[int, int, int]] = []

    def on_tick(elapsed: int, queue: tuple[int, int]) -> None:
        ticks.append((elapsed, queue[0], queue[1]))
        if elapsed % 30 < 5:
            print(f"        ... 已等待 {elapsed} 秒（队列：运行中 {queue[0]}，等待 {queue[1]}）")

    try:
        record = client.wait(prompt_id, timeout_seconds=timeout_seconds, poll_seconds=5, on_tick=on_tick)
    except comfy.ComfyError as exc:
        say("错误", "等待任务结束失败", str(exc))
        return 1

    elapsed_total = int(time.time() - started)
    status = comfy.ComfyClient.status_text(record)
    if status != "成功":
        messages = (record.get("status") or {}).get("messages") or []
        detail = "\n".join(str(m) for m in messages[-3:]) or "无更多信息"
        say("错误", f"任务未成功（{status}），耗时 {elapsed_total} 秒", detail)
        return 1
    say("OK", f"任务执行成功，耗时 {elapsed_total} 秒")

    # ---------------- 4. 取回并校验 ----------------
    files = comfy.ComfyClient.output_files(record, output_dir)
    if not files:
        say("错误", "任务成功但没找到输出文件", f"请到 ComfyUI 输出目录查看：{output_dir}")
        return 1
    ffprobe = settings.path("ffmpeg.ffprobe")
    results = []
    for path in files:
        if not path.exists():
            say("错误", f"输出文件不存在：{path}")
            continue
        try:
            info = probe_video(ffprobe, path)
        except Exception as exc:  # noqa: BLE001
            say("错误", f"ffprobe 校验失败：{path.name}", str(exc))
            continue
        fmt = info.get("format", {})
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        duration = float(fmt.get("duration", 0) or 0)
        size_mb = float(fmt.get("size", 0) or 0) / 1024**2
        results.append({"path": str(path), "duration": duration, "size_mb": round(size_mb, 2), "stream": stream})
        say("OK" if duration > 0.5 else "警告",
            f"输出：{path.name}",
            f"时长 {duration:.2f} 秒｜{stream.get('width')}x{stream.get('height')}｜"
            f"编码 {stream.get('codec_name')}｜{size_mb:.2f} MB｜{path}")

    # ---------------- 5. 归档到项目目录 ----------------
    archive_dir = str(values.get("output_prefix", "clips")).replace("\\", "/").strip("/").split("/")[-1] or "clips"
    archive_root = PROJECT_ROOT / "output/segments" / archive_dir
    archive_root.mkdir(parents=True, exist_ok=True)
    for item in results:
        src = Path(item["path"])
        target = archive_root / src.name
        if target.exists() and target.stat().st_size == src.stat().st_size:
            say("提示", f"归档文件已存在且大小一致，跳过：{target.name}")
            continue
        shutil.copy2(src, target)
        item["project_path"] = str(target)
        say("OK", f"已归档到项目：{target}")

    # ---------------- 6. 落盘运行记录 ----------------
    # 运行记录跟着归档目录走，这样不同测试/项目各归各处
    record_dir = PROJECT_ROOT / "data/projects" / archive_dir / "runs"
    record_dir.mkdir(parents=True, exist_ok=True)
    record_file = record_dir / f"{prompt_id}.json"
    record_file.write_text(json.dumps({
        "prompt_id": prompt_id,
        "target": args.target,
        "workflow": str(workflow_path),
        "prompt_source": prompt_source,
        "prompt_text": prompt_text,
        "overrides": {str(k): v for k, v in overrides.items()},
        "elapsed_seconds": elapsed_total,
        "status": status,
        "outputs": results,
        "api_prompt_nodes": len(api_prompt),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    say("OK", "运行记录已保存", str(record_file))
    print()
    print(f"结论：成功生成 {len(results)} 个视频文件，最长等待 {elapsed_total} 秒。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
