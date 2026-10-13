"""ComfyUI API 的最小客户端（阶段五）。

只负责：查状态、提交任务、等待完成、取回输出文件清单。
排队、重试、断点续跑属于阶段六的“任务管理器”，这里刻意不做。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import requests


class ComfyError(RuntimeError):
    """ComfyUI 交互失败，错误信息需包含可执行的解决建议。"""


def load_object_info(
    base_url: str,
    cache_file: Path,
    *,
    refresh: bool = False,
    max_age_hours: int = 12,
) -> dict:
    """读取 ComfyUI 的节点定义表（object_info），带本地缓存。

    工作流转换必须依赖它，用它来核对每个节点的真实输入名，不猜测参数。
    """
    if not refresh and cache_file.exists():
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # 缓存损坏则重新拉取
    url = base_url.rstrip("/") + "/object_info"
    try:
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ComfyError(
            f"无法获取节点定义表：{url}\n原因：{exc}\n"
            f"解决办法：确认 ComfyUI 已启动，且 config/settings.yaml 的 comfyui.base_url 正确。"
        ) from exc
    data = resp.json()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _explain_node_errors(payload: dict) -> str:
    """把 /prompt 返回的校验错误翻译成人话。"""
    lines: list[str] = []
    error = payload.get("error") or {}
    if isinstance(error, dict):
        lines.append(f"接口报错：{error.get('type', '未知')} / {error.get('message', '')}")
    node_errors = payload.get("node_errors") or {}
    if isinstance(node_errors, dict):
        for node_id, info in node_errors.items():
            items = info.get("errors", []) if isinstance(info, dict) else []
            for item in items:
                lines.append(
                    f"节点 {node_id}（{info.get('class_type', '?')}）：{item.get('message', '?')}"
                    + (f"｜{item.get('details', '')}" if item.get("details") else "")
                )
    return "\n".join(lines) or str(payload)[:500]


class ComfyClient:
    def __init__(self, base_url: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex

    # ---------------- 状态 ----------------

    def system_stats(self) -> dict:
        try:
            resp = requests.get(f"{self.base_url}/system_stats", timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ComfyError(
                f"无法访问 ComfyUI：{self.base_url}\n原因：{exc}\n"
                f"解决办法：确认 ComfyUI 已启动，且地址与 config/settings.yaml 的 comfyui.base_url 一致。"
            ) from exc
        return resp.json()

    def queue_state(self) -> tuple[int, int]:
        resp = requests.get(f"{self.base_url}/queue", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return len(data.get("queue_running", [])), len(data.get("queue_pending", []))

    # ---------------- 提交与等待 ----------------

    def submit(self, prompt: dict, extra_data: dict | None = None) -> str:
        body: dict[str, Any] = {"prompt": prompt, "client_id": self.client_id}
        if extra_data:
            body["extra_data"] = extra_data
        resp = requests.post(f"{self.base_url}/prompt", json=body, timeout=self.timeout)
        if resp.status_code != 200:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw": resp.text[:500]}
            raise ComfyError(
                f"提交任务被拒绝（HTTP {resp.status_code}）：\n{_explain_node_errors(payload)}\n"
                f"解决办法：按上面的节点号检查输入是否合法（多为模型名或尺寸不匹配）。"
            )
        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"提交后没有拿到 prompt_id，返回内容：{str(data)[:300]}")
        return str(prompt_id)

    def history(self, prompt_id: str) -> dict | None:
        resp = requests.get(f"{self.base_url}/history/{prompt_id}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get(prompt_id)

    def interrupt(self) -> None:
        try:
            requests.post(f"{self.base_url}/interrupt", timeout=self.timeout)
        except requests.RequestException:
            pass

    def wait(
        self,
        prompt_id: str,
        *,
        timeout_seconds: int,
        poll_seconds: float = 5.0,
        on_tick: Callable[[int, tuple[int, int]], None] | None = None,
    ) -> dict:
        """轮询直到任务结束；超时抛错（不会自动取消任务）。"""
        started = time.time()
        while True:
            elapsed = time.time() - started
            if elapsed > timeout_seconds:
                raise ComfyError(
                    f"等待超时（{timeout_seconds} 秒）仍未完成，任务可能仍在运行。\n"
                    f"prompt_id={prompt_id}\n"
                    f"解决办法：到 ComfyUI 界面查看队列；确认无误后可调大 "
                    f"config/settings.yaml 里的 comfyui.job_timeout_seconds。"
                )
            record = self.history(prompt_id)
            if record and record.get("status", {}).get("completed"):
                return record
            if on_tick:
                try:
                    on_tick(int(elapsed), self.queue_state())
                except requests.RequestException:
                    pass
            time.sleep(poll_seconds)

    # ---------------- 输出 ----------------

    @staticmethod
    def output_files(record: dict, output_dir: Path) -> list[Path]:
        """从历史记录里取出生成的文件路径。"""
        files: list[Path] = []
        for node_outputs in (record.get("outputs") or {}).values():
            if not isinstance(node_outputs, dict):
                continue
            for group in node_outputs.values():
                if not isinstance(group, list):
                    continue
                for item in group:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    files.append(
                        output_dir / str(item.get("subfolder", "")) / str(item["filename"])
                    )
        return files

    @staticmethod
    def status_text(record: dict) -> str:
        status = record.get("status", {})
        if status.get("completed"):
            return "成功"
        if status.get("status_str") == "error":
            return "失败"
        return str(status.get("status_str", "未知"))
