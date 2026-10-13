"""任务管理器（阶段六）：队列、日志、失败重试、断点续跑、任务去重。

设计要点
--------
* 每个项目一份台账 ``data/projects/<项目名>/segments.json``，记录每个片段的状态、
  尝试次数、prompt_id、产物路径与错误原因；
* 状态每步**原子落盘**（先写临时文件再替换），所以随时可以中断、重启；
* **提交成功立刻写盘 prompt_id**：重启后先按 prompt_id 查历史，已经跑完的直接收编，
  绝不重复占用显卡；
* **去重指纹** = 工作流内容哈希 + 提示词 + 时长 + 分辨率 + 种子（不含输出文件名），
  指纹命中历史成功记录就复用产物，不重新生成；
* 失败按指数退避重试，超过上限标 failed 并写清原因；单个片段失败不影响其它片段。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import comfy, generate
from . import workflow as wf_mod
from .config import PROJECT_ROOT

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REUSED = "reused"      # 指纹命中历史成功记录，直接复用产物


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _process_alive(pid: int) -> bool:
    """判断进程是否还在（Windows 上是尽力而为的判断）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True
    return True


@dataclass
class Segment:
    id: str
    prompt: str
    #: 提示词来源文件（相对项目根目录）。有它时**以文件内容为准**，
    #: 台账里的 prompt 只作为快照留档 —— 这样改完提示词文件重跑就会生效。
    prompt_file: str | None = None
    seed: int | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    #: 本段用到的参考图文件名（按顺序对应提示词里的 @图片1、@图片2…）；
    #: 用几张写几张，没写的槽位在提交时自动跳过。
    images: list[str] | None = None
    #: 首尾帧工作流用：本段的首帧 / 尾帧图片文件名
    first_frame: str | None = None
    last_frame: str | None = None
    #: 连续性锚点：指定"上一段的片段 id"，本段会把上一段的**真实最后一帧**
    #: 作为额外参考图加进槽位（T8 这类参考图工作流用它来抹平接缝）。
    chain_prev: str | None = None
    status: str = STATUS_PENDING
    attempts: int = 0
    prompt_id: str | None = None
    output_path: str | None = None
    fingerprint: str | None = None
    error: str | None = None
    elapsed_seconds: float | None = None
    updated_at: str | None = None
    history: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Project:
    name: str
    target: str = "t2v_4step"
    defaults: dict[str, Any] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            name=str(data.get("name") or "unnamed"),
            target=str(data.get("target") or "t2v_4step"),
            defaults=dict(data.get("defaults") or {}),
            segments=[Segment.from_dict(s) for s in (data.get("segments") or [])],
            created_at=str(data.get("created_at") or _now()),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for seg in self.segments:
            result[seg.status] = result.get(seg.status, 0) + 1
        return result


class ProjectStore:
    """项目台账的读写（原子写 + 单实例锁）。"""

    def __init__(self, project_dir: Path) -> None:
        self.dir = project_dir
        self.path = project_dir / "segments.json"
        self.lock_path = project_dir / ".runner.lock"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Project:
        if not self.path.exists():
            raise FileNotFoundError(
                f"项目台账不存在：{self.path}\n"
                f"解决办法：先用 scripts/run_batch.py --create 创建项目。"
            )
        return Project.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, project: Project) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)      # 原子替换，避免中断写坏台账

    def acquire_lock(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                pid = int(json.loads(self.lock_path.read_text(encoding="utf-8")).get("pid", -1))
            except (ValueError, json.JSONDecodeError, AttributeError):
                pid = -1
            if pid > 0 and pid != os.getpid() and _process_alive(pid):
                raise RuntimeError(
                    f"已有另一个任务管理器在运行（pid={pid}）。\n"
                    f"解决办法：等它结束；若确认它已崩溃，删除锁文件后重试：{self.lock_path}"
                )
        self.lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": _now()}),
                                  encoding="utf-8")

    def release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass


class GenerationIndex:
    """全局"已经生成过什么"的索引，用于任务去重。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def get(self, fingerprint: str) -> dict | None:
        return self._load().get(fingerprint)

    def put(self, fingerprint: str, record: dict) -> None:
        data = self._load()
        data[fingerprint] = {**record, "indexed_at": _now()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class TaskRunner:
    """按项目台账逐段生成，支持重试、续跑与去重。"""

    def __init__(
        self,
        *,
        client,
        ui,
        workflow_path: Path,
        target_cfg: dict,
        store: ProjectStore,
        index: GenerationIndex,
        output_dir: Path,
        archive_root: Path,
        ffprobe: Path | None = None,
        ffmpeg: Path | None = None,
        input_dir: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.ui = ui
        self.workflow_path = workflow_path
        self.target_cfg = target_cfg
        self.store = store
        self.index = index
        self.output_dir = output_dir
        self.archive_root = archive_root
        self.ffprobe = ffprobe
        self.ffmpeg = ffmpeg
        self.input_dir = input_dir
        self.log = logger or logging.getLogger("video_auto.tasks")

    def values_for(self, project: Project, seg: Segment) -> dict[str, Any]:
        """把项目默认值与片段自身参数合并成一次生成的完整参数。"""
        values: dict[str, Any] = dict(project.defaults)
        values["target"] = project.target
        for key in ("duration_seconds", "width", "height", "seed"):
            value = getattr(seg, key)
            if value is not None:
                values[key] = value
        values["prompt"] = self._effective_prompt(seg)
        # 参考图：按顺序填进 ref_image_1..9，缺的槽位由 generate.build_bypass 自动跳过
        for index in range(1, 10):
            key = f"ref_image_{index}"
            if seg.images and index <= len(seg.images):
                values[key] = seg.images[index - 1]
            else:
                values.pop(key, None)
        for key in ("first_frame", "last_frame"):
            value = getattr(seg, key)
            if value:
                values[key] = value
            else:
                values.pop(key, None)
        return values

    def _inject_chain_anchor(self, project: Project, seg: Segment, values: dict) -> None:
        """把上一段的真实最后一帧作为额外参考图塞进本段（连续性锚点）。

        这是"参考图工作流"抹平接缝的关键：下一段生成时能"看到"上一段结束时的
        真实画面，从而保持人物位置、构图与光线连续。
        """
        if not seg.chain_prev:
            return
        if not (self.ffmpeg and self.input_dir):
            self.log.warning("%s 需要连续性锚点，但缺少 ffmpeg/input_dir 配置，已跳过", seg.id)
            return
        prev = next((s for s in project.segments if s.id == seg.chain_prev), None)
        if prev is None or not prev.output_path or not Path(prev.output_path).exists():
            self.log.warning("%s 的锚点来源 %s 还没有产物，本段将按普通参考图生成",
                             seg.id, seg.chain_prev)
            return
        anchor_name = f"chain-{prev.id}.png"
        anchor_path = self.input_dir / anchor_name
        try:
            generate.extract_last_frame(self.ffmpeg, Path(prev.output_path), anchor_path)
        except (generate.GenerateError, OSError) as exc:
            # 抽帧失败不能让整批崩掉：退化成"不带锚点"继续生成
            self.log.warning("%s 注入连续性锚点失败，将不带锚点继续：%s", seg.id, exc)
            return
        used = len(seg.images or [])
        key = f"ref_image_{used + 1}"
        values[key] = anchor_name
        self.log.info("%s 已注入连续性锚点：%s（取自 %s）", seg.id, anchor_name, prev.id)

    def _effective_prompt(self, seg: Segment) -> str:
        """取本次生成实际使用的提示词：优先读文件，读不到再用台账里的快照。"""
        if not seg.prompt_file:
            return seg.prompt
        path = Path(seg.prompt_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text and text != seg.prompt:
                self.log.info("%s 使用提示词文件的最新内容：%s", seg.id, path)
            return text or seg.prompt
        self.log.warning("%s 的提示词文件不存在，改用台账快照：%s", seg.id, path)
        return seg.prompt

    def _output_prefix(self, project: Project, seg: Segment) -> str:
        return f"video_auto/{project.name}/{seg.id}"

    def run(
        self,
        project: Project,
        *,
        max_attempts: int = 3,
        retry_backoff: float = 10.0,
        timeout_seconds: int = 3600,
        reuse_existing: bool = True,
        stop_on_error: bool = False,
        dry_run: bool = False,
    ) -> dict[str, int]:
        for seg in project.segments:
            if seg.status == STATUS_DONE:
                self.log.info("跳过（已完成）：%s", seg.id)
                continue

            values = self.values_for(project, seg)
            fingerprint = generate.segment_fingerprint(self.workflow_path, values)
            seg.fingerprint = fingerprint

            if reuse_existing:
                hit = self.index.get(fingerprint)
                if hit and Path(str(hit.get("path", ""))).exists():
                    seg.output_path = str(hit["path"])
                    seg.status = STATUS_REUSED
                    seg.updated_at = _now()
                    seg.history.append({"at": _now(), "event": "reuse",
                                        "from_prompt_id": hit.get("prompt_id")})
                    self.store.save(project)
                    self.log.info("复用已有结果（指纹命中）：%s -> %s", seg.id, hit["path"])
                    continue

            if dry_run:
                self.log.info("dry-run：%s 需要生成（指纹 %s…）", seg.id, fingerprint[:12])
                continue

            if seg.prompt_id:
                if self._adopt_finished(project, seg, values):
                    continue
                self.log.warning("%s 上次提交的任务未完成，将重新提交：prompt_id=%s",
                                 seg.id, seg.prompt_id)
                seg.prompt_id = None

            self._inject_chain_anchor(project, seg, values)
            self._attempt_loop(project, seg, values, max_attempts=max_attempts,
                               retry_backoff=retry_backoff, timeout_seconds=timeout_seconds,
                               fingerprint=fingerprint)

            if stop_on_error and seg.status == STATUS_FAILED:
                self.log.error("已按 stop_on_error 中止后续片段")
                break

        self.store.save(project)
        return project.counts()

    def _adopt_finished(self, project: Project, seg: Segment, values: dict) -> bool:
        """断点续跑的关键：上次已提交的任务如果已经跑完，直接收编，不重复生成。"""
        try:
            record = self.client.history(str(seg.prompt_id))
        except comfy.ComfyError as exc:
            self.log.warning("查询历史失败，稍后会重新提交：%s", exc)
            return False
        if not record or not record.get("status", {}).get("completed"):
            return False
        try:
            outputs = self._collect_from_history(record, seg)
        except generate.GenerateError as exc:
            seg.error = str(exc)
            self.log.warning("%s 虽已完成但收编失败：%s", seg.id, exc)
            return False
        seg.status = STATUS_DONE
        seg.updated_at = _now()
        seg.history.append({"at": _now(), "event": "resume_adopt",
                            "prompt_id": seg.prompt_id, "outputs": outputs})
        self.store.save(project)
        self.log.info("续跑收编成功：%s（prompt_id=%s）", seg.id, seg.prompt_id)
        return True

    def _attempt_loop(self, project: Project, seg: Segment, values: dict, *,
                      max_attempts: int, retry_backoff: float,
                      timeout_seconds: int, fingerprint: str) -> None:
        while seg.attempts < max_attempts:
            seg.attempts += 1
            seg.status = STATUS_RUNNING
            seg.error = None
            seg.updated_at = _now()
            self.store.save(project)      # 先落盘，保证重启后知道在做什么
            self.log.info("开始生成 %s（第 %d/%d 次尝试）", seg.id, seg.attempts, max_attempts)

            def on_submitted(prompt_id: str, seg: Segment = seg) -> None:
                seg.prompt_id = prompt_id
                self.store.save(project)
                self.log.info("已提交 %s：prompt_id=%s", seg.id, prompt_id)

            try:
                result = generate.generate_clip(
                    self.client,
                    self.ui,
                    self.target_cfg,
                    {**values, "output_prefix": self._output_prefix(project, seg)},
                    output_dir=self.output_dir,
                    timeout_seconds=timeout_seconds,
                    ffprobe=self.ffprobe,
                    archive_root=self.archive_root,
                    on_submitted=on_submitted,
                    on_tick=lambda elapsed, q, seg=seg: self.log.debug(
                        "%s 等待中 %ss（队列 %s）", seg.id, elapsed, q),
                )
            except wf_mod.ConvertError as exc:
                # 工作流转换/参数改写的问题：重试不会变好，直接判失败并保留原因
                seg.error = str(exc)
                seg.status = STATUS_FAILED
                seg.updated_at = _now()
                seg.history.append({"at": _now(), "event": "convert_failed",
                                    "attempt": seg.attempts, "error": str(exc)[:800]})
                self.store.save(project)
                self.log.error("%s 工作流转换失败（不重试）：%s", seg.id, str(exc)[:600])
                return
            except (comfy.ComfyError, generate.GenerateError) as exc:
                seg.error = str(exc)
                seg.updated_at = _now()
                seg.history.append({"at": _now(), "event": "attempt_failed",
                                    "attempt": seg.attempts, "error": str(exc)[:500]})
                self.store.save(project)
                self.log.error("%s 第 %d 次尝试失败：%s", seg.id, seg.attempts, str(exc)[:400])
                if seg.attempts >= max_attempts:
                    seg.status = STATUS_FAILED
                    self.store.save(project)
                    self.log.error("%s 已达最大尝试次数（%d），标记为失败", seg.id, max_attempts)
                    return
                wait = retry_backoff * (2 ** (seg.attempts - 1))
                self.log.info("等待 %.0f 秒后重试 %s", wait, seg.id)
                time.sleep(wait)
                continue

            self._apply_result(seg, result)
            seg.status = STATUS_DONE
            seg.updated_at = _now()
            seg.history.append({"at": _now(), "event": "attempt_ok", "attempt": seg.attempts,
                                "prompt_id": result.prompt_id, "outputs": result.outputs})
            self.store.save(project)
            self.index.put(fingerprint, {
                "prompt_id": result.prompt_id,
                "path": seg.output_path,
                "outputs": result.outputs,
                "project": project.name,
                "segment": seg.id,
            })
            self.log.info("完成 %s：%s（耗时 %.1f 秒）", seg.id, seg.output_path, result.elapsed_seconds)
            return

    def _apply_result(self, seg: Segment, result) -> list[dict]:
        outputs = getattr(result, "outputs", None) or []
        if not outputs:
            raise generate.GenerateError(
                f"{seg.id} 没有可用的输出文件。\n"
                f"解决办法：检查 ComfyUI 输出目录与工作流的保存节点。"
            )
        seg.output_path = outputs[0].get("path") or outputs[0].get("comfy_path")
        seg.elapsed_seconds = getattr(result, "elapsed_seconds", None)
        return outputs

    def _collect_from_history(self, record: dict, seg: Segment) -> list[dict]:
        """从历史记录收编产物（续跑时用）。"""
        files = comfy.ComfyClient.output_files(record, self.output_dir)
        existing = [f for f in files if f.exists()]
        if not existing:
            raise generate.GenerateError(
                f"{seg.id} 的任务已完成，但输出文件不在磁盘上。\n"
                f"解决办法：确认 ComfyUI 输出目录未变动：{self.output_dir}"
            )
        archived = generate.archive_outputs(existing, self.archive_root)
        outputs: list[dict] = []
        for info in archived:
            item: dict[str, Any] = {"comfy_path": info["source"], "path": info["path"]}
            if self.ffprobe and Path(info["path"]).exists():
                item.update(generate.summarize_video(
                    generate.verify_video(self.ffprobe, Path(info["path"]))))
            outputs.append(item)
        seg.output_path = outputs[0]["path"]
        return outputs
