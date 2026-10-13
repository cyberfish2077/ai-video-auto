"""单个视频片段的生成流程（阶段六起，试跑脚本与批量任务管理器共用）。

把"改写参数 → 转 API 格式 → 提交 → 等待 → 校验 → 归档"这段逻辑收敛在一处，
避免两套代码各自演化、行为不一致。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import comfy, workflow as wf_mod


class GenerateError(RuntimeError):
    """单片段生成失败，错误信息需说明原因与下一步动作。"""


@dataclass
class ClipResult:
    prompt_id: str
    status: str
    elapsed_seconds: float
    api_prompt_nodes: int = 0
    outputs: list[dict] = field(default_factory=list)


def build_overrides(target_cfg: dict, values: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """按 config/workflow_targets.yaml 的 overrides 翻译成 ``{节点id: {控件名: 值}}``。"""
    overrides: dict[int, dict[str, Any]] = {}
    overrides_cfg = target_cfg.get("overrides") or {}
    for key, value in merge_target_defaults(target_cfg, values).items():
        spec = overrides_cfg.get(key)
        if not spec:
            continue
        # 一个业务参数可以对应多个节点（例如首帧缩放的 area 要同时改两处）
        specs = spec if isinstance(spec, list) else [spec]
        for item in specs:
            if isinstance(item, dict) and "node" in item:
                overrides.setdefault(int(item["node"]), {})[str(item["input"])] = value
    return overrides


def merge_target_defaults(target_cfg: dict, values: dict[str, Any]) -> dict[str, Any]:
    """把工作流自带的固定参数（例如文本编码器）合并进本次生成的参数。

    优先级：工作流 defaults < 项目/片段 values。
    这样"这个工作流固定用哪个编码器"只需要在配置里写一次。
    """
    return {**(target_cfg.get("defaults") or {}), **values}


def build_bypass(target_cfg: dict, values: dict[str, Any]) -> set[int]:
    """返回需要跳过的"空参考图槽位"节点。

    工作流里 9 个参考图槽位默认全部启用，但每个片段实际用几张图由内容决定。
    没给图的槽位必须跳过，否则 ComfyUI 会因为找不到文件而拒绝提交。
    """
    bypass: set[int] = set()
    values = merge_target_defaults(target_cfg, values)
    for key, spec in (target_cfg.get("overrides") or {}).items():
        if not str(key).startswith("ref_image_"):
            continue
        if values.get(key):
            continue
        if isinstance(spec, dict) and "node" in spec:
            bypass.add(int(spec["node"]))
    return bypass


#: 需要做"视频解码校验"的扩展名；图片只记录路径与大小
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def segment_fingerprint(workflow_file: Path, values: dict[str, Any]) -> str:
    """内容指纹：用来判断"同样内容是否已经生成过"。

    刻意**不包含**输出文件名前缀 —— 换个名字重新导出并不会改变画面内容。
    """
    payload = {
        "workflow_sha256": hashlib.sha256(workflow_file.read_bytes()).hexdigest(),
        "prompt": values.get("prompt"),
        "duration_seconds": values.get("duration_seconds"),
        "width": values.get("width"),
        "height": values.get("height"),
        "seed": values.get("seed"),
        "target": values.get("target"),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify_video(ffprobe: Path, path: Path) -> dict:
    """用 ffprobe 校验文件能正常解码，返回格式与流信息。"""
    cmd = [
        str(ffprobe), "-v", "error",
        "-show_entries",
        "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,nb_frames",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise GenerateError(f"ffprobe 校验超时：{path}") from exc
    if proc.returncode != 0:
        raise GenerateError(
            f"ffprobe 无法解码该文件：{path}\n{(proc.stderr or '').strip()[:300]}\n"
            f"解决办法：确认文件未损坏；必要时检查生成阶段是否报错。"
        )
    return json.loads(proc.stdout or "{}")


def summarize_video(info: dict) -> dict:
    """把 ffprobe 输出压成便于记录的一行摘要。"""
    fmt = info.get("format", {}) or {}
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "duration": round(float(fmt.get("duration", 0) or 0), 3),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "frames": video.get("nb_frames"),
        "video_codec": video.get("codec_name"),
        "has_audio": bool(audio),
        "audio_codec": audio.get("codec_name"),
    }


def archive_outputs(files: list[Path], archive_root: Path) -> list[dict]:
    """把 ComfyUI 的输出复制到项目目录，返回归档信息；同名同大小则跳过复制。"""
    archive_root.mkdir(parents=True, exist_ok=True)
    archived: list[dict] = []
    for src in files:
        target = archive_root / src.name
        already = target.exists() and target.stat().st_size == src.stat().st_size
        if not already:
            shutil.copy2(src, target)
        archived.append({"source": str(src), "path": str(target), "copied": not already})
    return archived


def extract_last_frame(ffmpeg: Path, video: Path, dest: Path, *, offset: float = 0.06) -> Path:
    """取视频的最后一帧写成图片（用于把上一段的结尾作为下一段的连续性锚点）。

    用 ``-sseof`` 从结尾往前 seek 一点点，避免落在最后一帧之后取不到图。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-sseof", f"-{offset:.3f}", "-i", str(video),
        "-frames:v", "1", "-update", "1", str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    if proc.returncode != 0 or not dest.exists():
        raise GenerateError(
            f"提取 {video.name} 的最后一帧失败。\n{(proc.stderr or '').strip()[-300:]}\n"
            f"解决办法：确认 config/settings.yaml 里的 ffmpeg 路径正确。"
        )
    return dest


def generate_clip(
    client,
    ui: wf_mod.UiWorkflow,
    target_cfg: dict,
    values: dict[str, Any],
    *,
    output_dir: Path,
    timeout_seconds: int,
    poll_seconds: float = 5.0,
    ffprobe: Path | None = None,
    archive_root: Path | None = None,
    resume_prompt_id: str | None = None,
    on_submitted: Callable[[str], None] | None = None,
    on_tick: Callable[[int, tuple[int, int]], None] | None = None,
) -> ClipResult:
    """生成一个片段；``resume_prompt_id`` 用于重启后接着监控已提交的任务。"""
    api_prompt = ui.to_api_prompt(
        build_overrides(target_cfg, values),
        bypass_nodes=build_bypass(target_cfg, values),
    )
    if resume_prompt_id:
        prompt_id = resume_prompt_id
    else:
        prompt_id = client.submit(api_prompt, extra_data={"extra_pnginfo": {"source": "video_auto"}})
        if on_submitted:
            on_submitted(prompt_id)   # 立刻落盘 prompt_id，重启后能接着查

    started = time.time()
    record = client.wait(prompt_id, timeout_seconds=timeout_seconds,
                         poll_seconds=poll_seconds, on_tick=on_tick)
    elapsed = round(time.time() - started, 1)

    status = comfy.ComfyClient.status_text(record)
    if status != "成功":
        messages = (record.get("status") or {}).get("messages") or []
        detail = "｜".join(str(m) for m in messages[-2:])
        raise GenerateError(f"任务未成功（{status}）：{detail or '无更多信息'}｜prompt_id={prompt_id}")

    files = comfy.ComfyClient.output_files(record, output_dir)
    existing = [f for f in files if f.exists()]
    if not existing:
        raise GenerateError(
            f"任务成功但没找到输出文件（outputs={record.get('outputs')}）。\n"
            f"解决办法：到 ComfyUI 输出目录确认：{output_dir}"
        )

    outputs: list[dict] = []
    for path in existing:
        item: dict[str, Any] = {"comfy_path": str(path)}
        if ffprobe:
            if path.suffix.lower() in VIDEO_SUFFIXES:
                item.update(summarize_video(verify_video(ffprobe, path)))
            else:
                # 图片（例如 Qwen-Image 生成的参考图）不做解码校验，只记大小与类型
                item.update({
                    "size_bytes": path.stat().st_size,
                    "kind": "image" if path.suffix.lower() in IMAGE_SUFFIXES else "other",
                })
        outputs.append(item)
    if archive_root:
        archived = archive_outputs(existing, archive_root)
        by_source = {a["source"]: a for a in archived}
        for item in outputs:
            info = by_source.get(item["comfy_path"])
            if info:
                item["path"] = info["path"]

    return ClipResult(prompt_id=prompt_id, status=status, elapsed_seconds=elapsed,
                      api_prompt_nodes=len(api_prompt), outputs=outputs)
