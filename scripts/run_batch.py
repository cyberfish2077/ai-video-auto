"""批量生成入口（阶段六）。

动作（同时只能选一个）：

    create   用提示词文件创建/重建项目台账
    status   查看项目进度
    dry-run  只排出计划：哪些片段要生成、哪些能复用已有结果（不占用显卡）
    run      真正执行（支持中断后重跑，自动续跑与去重）
    reset    把 失败/运行中 的片段退回待生成
    regen    受控重生成已完成片段：备份台账 + 归档旧产物 + 可换种子/提示词
    backfill-index  把历史运行记录补进"去重索引"，避免重复生成

用法示例（在项目根目录执行）::

    # 用两个提示词文件建一个项目，时长 5 秒、960x544
    .venv\\Scripts\\python.exe scripts\\run_batch.py create --project batch-01 ^
        --prompt-file data/projects/smoke-01/prompt.txt ^
        --prompt-file data/projects/smoke-02/prompt.txt --seed 1001

    .venv\\Scripts\\python.exe scripts\\run_batch.py status --project batch-01
    .venv\\Scripts\\python.exe scripts\\run_batch.py dry-run --project batch-01
    .venv\\Scripts\\python.exe scripts\\run_batch.py run --project batch-01
"""

from __future__ import annotations

import argparse
import json
import shutil
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

from video_auto import comfy, config as cfg, generate, logging_util  # noqa: E402
from video_auto import tasks as task_mod  # noqa: E402
from video_auto import workflow as wf_mod  # noqa: E402

LOG_NAME = "batch"


def project_dir(settings, name: str) -> Path:
    return settings.path("paths.projects") / name


def cmd_create(args, settings, log) -> int:
    if args.from_json:
        return _create_from_json(args, settings, log)
    files = [Path(p) for p in (args.prompt_file or [])]
    if not files:
        log.error("至少要给一个 --prompt-file")
        return 2
    prompts: list[tuple[str, str]] = []
    for path in files:
        if not path.exists():
            log.error("提示词文件不存在：%s", path)
            return 2
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            log.error("提示词文件是空的：%s", path)
            return 2
        prompts.append((path.stem, text))

    segments: list[task_mod.Segment] = []
    base_seed = args.seed
    for index, (label, text) in enumerate(prompts, start=1):
        segments.append(task_mod.Segment(
            id=f"seg-{index:02d}",
            prompt=text,
            seed=(base_seed + index - 1) if base_seed is not None else None,
            duration_seconds=args.duration,
            width=args.width,
            height=args.height,
        ))
    project = task_mod.Project(
        name=args.project,
        target=args.target,
        defaults={"duration_seconds": args.duration, "width": args.width, "height": args.height},
        segments=segments,
    )
    store = task_mod.ProjectStore(project_dir(settings, args.project))
    if store.exists() and not args.force:
        log.error("项目已存在：%s（要重建请加 --force）", store.path)
        return 2
    store.save(project)
    log.info("已创建项目 %s：%d 个片段 -> %s", project.name, len(segments), store.path)
    for seg in segments:
        log.info("  %s  seed=%s  提示词 %d 字：%s", seg.id, seg.seed, len(seg.prompt),
                 seg.prompt[:48].replace("\n", " ") + "…")
    return 0


def _create_from_json(args, settings, log) -> int:
    """按 JSON 定义创建项目。

    格式（阶段八 DeepSeek 的分段结果可直接转成这个）::

        {
          "name": "batch-01",
          "target": "t2v_4step",
          "defaults": {"duration_seconds": 5, "width": 960, "height": 544},
          "segments": [
            {"id": "seg-01", "prompt_file": "data/projects/smoke-01/prompt.txt", "seed": 1234},
            {"id": "seg-02", "prompt": "直接写提示词", "seed": 20261012, "duration_seconds": 6}
          ]
        }
    """
    path = Path(str(args.from_json))
    if not path.exists():
        log.error("JSON 定义文件不存在：%s", path)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("JSON 解析失败：%s\n%s", path, exc)
        return 2

    name = args.project or data.get("name")
    if not name:
        log.error("JSON 里没有 name，命令行也没给 --project")
        return 2

    defaults = dict(data.get("defaults") or {})
    defaults.setdefault("duration_seconds", args.duration)
    defaults.setdefault("width", args.width)
    defaults.setdefault("height", args.height)

    segments: list[task_mod.Segment] = []
    for index, item in enumerate(data.get("segments") or [], start=1):
        text = item.get("prompt")
        if not text and item.get("prompt_file"):
            candidate = Path(str(item["prompt_file"]))
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / candidate
            if not candidate.exists():
                log.error("第 %d 段的提示词文件不存在：%s", index, candidate)
                return 2
            text = candidate.read_text(encoding="utf-8").strip()
        if not text:
            log.error("第 %d 段既没有 prompt 也没有可用的 prompt_file", index)
            return 2
        segments.append(task_mod.Segment(
            id=str(item.get("id") or f"seg-{index:02d}"),
            prompt=text,
            prompt_file=str(item["prompt_file"]) if item.get("prompt_file") else None,
            seed=item.get("seed"),
            duration_seconds=item.get("duration_seconds"),
            width=item.get("width"),
            height=item.get("height"),
            images=[str(x) for x in (item.get("images") or [])] or None,
            first_frame=str(item["first_frame"]) if item.get("first_frame") else None,
            last_frame=str(item["last_frame"]) if item.get("last_frame") else None,
            chain_prev=str(item["chain_prev"]) if item.get("chain_prev") else None,
        ))
    if not segments:
        log.error("JSON 里没有 segments")
        return 2

    project = task_mod.Project(
        name=str(name),
        target=args.target or str(data.get("target") or "t2v_4step"),
        defaults=defaults,
        segments=segments,
    )
    store = task_mod.ProjectStore(project_dir(settings, str(name)))
    if store.exists() and not args.force:
        log.error("项目已存在：%s（要重建请加 --force）", store.path)
        return 2
    store.save(project)
    log.info("已创建项目 %s：%d 个片段 -> %s", project.name, len(segments), store.path)
    print()
    return _print_status(project)


def cmd_reset(args, settings, log) -> int:
    """把 failed / running 的片段重置为 pending，让它们能被重新尝试。

    程序被中断后，正在跑的片段会停在 running；用尽尝试次数的会停在 failed。
    这个动作把它们清零重来，不删除已生成的产物。
    """
    store = task_mod.ProjectStore(project_dir(settings, args.project))
    if not store.exists():
        log.error("项目不存在：%s", store.path)
        return 2
    project = store.load()
    if args.id:
        # 指定片段时允许重置任何状态 —— 改了提示词要重新生成时用这个
        targets = [seg for seg in project.segments if seg.id == args.id]
        if not targets:
            log.error("项目里没有名为 %s 的片段（可选：%s）",
                      args.id, [s.id for s in project.segments])
            return 2
    else:
        targets = [
            seg for seg in project.segments
            if seg.status in (task_mod.STATUS_FAILED, task_mod.STATUS_RUNNING)
        ]
    if not targets:
        log.info("没有处于 失败/运行中 状态的片段，无需重置")
        return _print_status(project)
    for seg in targets:
        log.info("重置 %s（原状态 %s，已尝试 %d 次）", seg.id, seg.status, seg.attempts)
        seg.status = task_mod.STATUS_PENDING
        seg.attempts = 0
        seg.error = None
        seg.prompt_id = None
        seg.updated_at = task_mod._now()
        seg.history.append({"at": task_mod._now(), "event": "reset"})
    store.save(project)
    print()
    return _print_status(project)


def cmd_regen(args, settings, log) -> int:
    """受控重生成：把指定片段退回 pending，并归档旧产物（阶段八收尾新增）。

    与 ``reset`` 的区别，也是加这个动作的原因：

    * 先**备份台账**到 ``analysis/backup/``，手改坏了能取回；
    * 把旧产物**移动**到 ``segments/_superseded/``（移动不是删除，可随时取回），
      避免新旧产物混在一起分不清；
    * 可以顺手换种子（``--seed``）或换提示词文件（``--prompt-file``，仅单片段）；
    * 跑完告诉你下一步该执行什么命令。

    注意：连续性锚点取自"上一段的最后一帧"，所以如果同时重生成相邻两段，
    按台账顺序依次跑即可（``run`` 会自动按顺序执行）。
    """
    if not args.id:
        log.error("regen 必须用 --id 指定要重生成的片段（多个用逗号分隔：seg-04,seg-05）。\n"
                  "解决办法：先用 status 看清片段名；只想重置失败/中断的片段请用 reset。")
        return 2

    store = task_mod.ProjectStore(project_dir(settings, args.project))
    if not store.exists():
        log.error("项目不存在：%s", store.path)
        return 2
    project = store.load()

    wanted = [piece.strip() for piece in args.id.split(",") if piece.strip()]
    by_id = {seg.id: seg for seg in project.segments}
    missing = [sid for sid in wanted if sid not in by_id]
    if missing:
        log.error("项目里没有这些片段：%s（可选：%s）",
                  "、".join(missing), [s.id for s in project.segments])
        return 2

    prompt_file = None
    if args.prompt_file:
        if len(wanted) != 1:
            log.error("--prompt-file 只能和单个 --id 一起用（要批量改提示词请手改台账）。")
            return 2
        candidate = Path(args.prompt_file[0])
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        if not candidate.exists():
            log.error("提示词文件不存在：%s", candidate)
            return 2
        prompt_file = candidate

    if args.seed is not None and len(wanted) > 1:
        log.info("给了 --seed：按顺序依次使用 %d、%d、…", args.seed, args.seed + 1)

    project_dir_path = project_dir(settings, args.project)
    archive_dir = project_dir_path / "segments" / "_superseded"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print("=" * 78)
    print(f"受控重生成　项目：{args.project}　片段：{'、'.join(wanted)}")
    if args.plan:
        print("（--plan：只演练，不改任何东西）")
    print("=" * 78)

    backup_path = None
    if not args.plan:
        backup_dir = project_dir_path / "analysis" / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"segments.json.bak-regen-{stamp}"
        shutil.copy2(store.path, backup_path)
        log.info("台账已备份：%s", backup_path)

    for index, sid in enumerate(wanted):
        seg = by_id[sid]
        old_seed = seg.seed
        new_seed = args.seed + index if args.seed is not None else old_seed
        old_output = seg.output_path
        moved_to = None
        if old_output and Path(old_output).exists():
            source = Path(old_output)
            archive_dir.mkdir(parents=True, exist_ok=True)
            target = archive_dir / source.name
            if target.exists():
                target = archive_dir / f"{source.stem}_{stamp}{source.suffix}"
            if args.plan:
                moved_to = f"{target}（演练，未移动）"
            else:
                shutil.move(str(source), str(target))
                moved_to = str(target)
        print(f"  片段 {sid}：状态 {seg.status} → pending；"
              f"尝试次数 {seg.attempts} → 0；种子 {old_seed} → {new_seed}")
        if moved_to:
            print(f"          旧产物已归档：{moved_to}")
        if prompt_file:
            print(f"          提示词改用：{prompt_file}")
        if not args.plan:
            seg.status = task_mod.STATUS_PENDING
            seg.attempts = 0
            seg.prompt_id = None
            seg.error = None
            seg.elapsed_seconds = None
            seg.output_path = None
            seg.seed = new_seed
            if prompt_file:
                seg.prompt_file = str(prompt_file)
            seg.updated_at = task_mod._now()
            seg.history.append({
                "at": task_mod._now(), "event": "regen_requested",
                "reason": args.reason or "人工要求重生成",
                "superseded_output": old_output, "archived_to": moved_to,
                "old_seed": old_seed, "new_seed": new_seed,
                "prompt_file": str(prompt_file) if prompt_file else seg.prompt_file,
            })
    if not args.plan:
        store.save(project)
        print()
        print(f"台账已更新（备份：{backup_path}）")
    print()
    print("下一步：")
    print(f"  1) 确认 ComfyUI 在线：.venv\\Scripts\\python.exe scripts\\check_env.py")
    print(f"  2) 重跑：.venv\\Scripts\\python.exe scripts\\run_batch.py run --project {args.project}")
    print(f"  3) 复检字幕：.venv\\Scripts\\python.exe scripts\\check_segment_text.py "
          f"--project {args.project} --write-report")
    return 0


def cmd_status(args, settings, log) -> int:
    store = task_mod.ProjectStore(project_dir(settings, args.project))
    if not store.exists():
        log.error("项目不存在：%s", store.path)
        return 2
    project = store.load()
    return _print_status(project)


def _print_status(project: task_mod.Project) -> int:
    print(f"项目：{project.name}  工作流：{project.target}  创建：{project.created_at}")
    print(f"统计：{project.counts()}")
    print("-" * 78)
    header = f"{'片段':8s} {'状态':8s} {'次数':4s} {'耗时':8s} {'产物'}"
    print(header)
    for seg in project.segments:
        art = seg.output_path or ""
        if art:
            art = Path(art).name
        print(f"{seg.id:8s} {seg.status:8s} {seg.attempts:<4d} "
              f"{(str(seg.elapsed_seconds or '') + 's'):8s} {art}")
        if seg.error:
            print(f"         错误：{seg.error.splitlines()[0][:100]}")
    return 0


def _load_runtime(args, settings, log):
    """准备客户端、工作流、目标配置、台账与索引。"""
    targets = cfg.load_workflow_targets()
    store = task_mod.ProjectStore(project_dir(settings, args.project))
    if not store.exists():
        log.error("项目不存在：%s（先用 create 创建）", store.path)
        return None
    project = store.load()
    target_name = args.target or project.target
    if target_name not in targets:
        log.error("配置里没有工作流 %s（可选：%s）", target_name, sorted(targets))
        return None
    target_cfg = targets[target_name]
    workflow_path = settings.path("paths.workflows") / Path(str(target_cfg["file"])).name
    if not workflow_path.exists():
        log.error("工作流副本不存在：%s", workflow_path)
        return None
    object_info = comfy.load_object_info(str(settings.get("comfyui.base_url")),
                                        settings.path("paths.cache") / "object_info_cache.json")
    ui = wf_mod.load_ui_workflow(workflow_path, object_info)
    client = comfy.ComfyClient(str(settings.get("comfyui.base_url")), timeout=60)
    index = task_mod.GenerationIndex(settings.path("paths.cache") / "generation_index.json")
    # 产物按"工作流类型"归档到本次制作目录下：素材 -> materials/，分段视频 -> segments/
    subdir = str(target_cfg.get("archive_subdir") or "artifacts")
    archive_root = settings.path("paths.projects") / project.name / subdir
    runner = task_mod.TaskRunner(
        client=client, ui=ui, workflow_path=workflow_path, target_cfg=target_cfg,
        store=store, index=index, output_dir=settings.path("comfyui.output_dir"),
        archive_root=archive_root, ffprobe=settings.path("ffmpeg.ffprobe"), logger=log,
        ffmpeg=settings.path("ffmpeg.ffmpeg"), input_dir=settings.path("comfyui.input_dir"),
    )
    return project, store, runner


def cmd_plan_or_run(args, settings, log, *, dry_run: bool) -> int:
    runtime = _load_runtime(args, settings, log)
    if runtime is None:
        return 2
    project, store, runner = runtime
    store.acquire_lock()
    try:
        running, pending = runner.client.queue_state()
        if running or pending:
            log.warning("ComfyUI 队列非空闲（运行中 %d，等待 %d）", running, pending)
        counts = runner.run(
            project,
            max_attempts=args.max_attempts,
            retry_backoff=args.retry_backoff,
            timeout_seconds=args.max_wait,
            reuse_existing=not args.no_reuse,
            stop_on_error=args.stop_on_error,
            dry_run=dry_run,
        )
    finally:
        store.release_lock()
    log.info("完成。片段统计：%s", counts)
    if counts.get(task_mod.STATUS_FAILED):
        log.error("有 %d 个片段失败，详见：%s", counts[task_mod.STATUS_FAILED], store.path)
        return 1
    return 0


def cmd_backfill(args, settings, log) -> int:
    """把已有运行记录补进去重索引，这样重复的片段不会再花显卡重跑。"""
    targets = cfg.load_workflow_targets()
    index = task_mod.GenerationIndex(settings.path("paths.cache") / "generation_index.json")
    added = 0
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    roots = [settings.path("paths.projects"), settings.path("paths.legacy_projects")]
    record_files = sorted(
        f for root in roots if root.exists() for f in root.glob("*/runs/*.json")
    )
    for record_file in record_files:
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            skip("JSON 内容损坏")
            continue
        target_name = record.get("target")
        target_cfg = targets.get(target_name)
        workflow_file = (
            settings.path("paths.workflows") / Path(str(target_cfg["file"])).name
            if target_cfg else None
        )
        if not target_cfg or not workflow_file or not workflow_file.exists():
            skip(f"找不到对应工作流（target={target_name}）")
            continue
        prompt = record.get("prompt_text")
        outputs = record.get("outputs") or []
        if not prompt or not outputs:
            skip("记录里缺少提示词或产物")
            continue
        # 把记录里的改写点反查回业务参数名
        reverse: dict[tuple[str, str], str] = {}
        for key, spec in (target_cfg.get("overrides") or {}).items():
            reverse[(str(spec["node"]), str(spec["input"]))] = key
        values: dict = {"target": target_name, "prompt": prompt}
        # 兼容两种记录写法：
        #   嵌套式 {"159": {"value": "..."}}（批量脚本自动生成）
        #   扁平式 {"159.value": "..."}（早期手工补记）
        for node_key, payload in (record.get("overrides") or {}).items():
            if "." in node_key and not isinstance(payload, dict):
                node_id, input_name = node_key.split(".", 1)
                pairs = [(str(node_id), str(input_name), payload)]
            elif isinstance(payload, dict):
                pairs = [(str(node_key), str(k), v) for k, v in payload.items()]
            else:
                skip(f"改写点格式无法识别（{node_key}）")
                continue
            for node_id, input_name, value in pairs:
                name = reverse.get((node_id, input_name))
                if name and name in ("prompt", "prompt_switch"):
                    continue
                if name:
                    values[name] = value
        # 产物路径：兼容两种记录写法，必须文件真的存在
        path = None
        for item in outputs:
            candidate = item.get("project_path") or item.get("path") or item.get("comfy_path")
            if candidate and Path(str(candidate)).exists():
                path = str(candidate)
                break
        if not path:
            skip("记录的产物文件已不在磁盘上")
            continue
        fingerprint = generate.segment_fingerprint(workflow_file, values)
        index.put(fingerprint, {
            "prompt_id": record.get("prompt_id"),
            "path": path,
            "outputs": outputs,
            "source": f"backfill:{record_file.name}",
        })
        added += 1
        log.info("已登记：%s -> %s（指纹 %s…）", record_file.parent.parent.name,
                 Path(path).name, fingerprint[:12])
    log.info("补录完成：新增 %d 条，跳过 %d 条", added, sum(skipped.values()))
    for reason, count in sorted(skipped.items()):
        log.warning("  跳过 %d 条：%s", count, reason)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成入口（阶段六）")
    parser.add_argument("action",
                        choices=["create", "status", "dry-run", "run", "reset", "regen",
                                 "backfill-index"])
    parser.add_argument("--project", help="项目名（对应 data/projects/<名字>），backfill-index 不需要")
    parser.add_argument("--id", help="只对某个片段生效（用于 create 之外的动作）")
    parser.add_argument("--target", help="config/workflow_targets.yaml 里的工作流名")
    parser.add_argument("--prompt-file", action="append", help="提示词文件，可重复给多个")
    parser.add_argument("--from-json", help="按 JSON 定义创建项目（与 --prompt-file 二选一）")
    parser.add_argument("--seed", type=int, help="首个片段的种子，后续片段依次 +1")
    parser.add_argument("--duration", type=float, default=5.0, help="每个片段时长（秒），默认 5")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=544)
    parser.add_argument("--force", action="store_true", help="create 时覆盖已有台账")
    parser.add_argument("--max-attempts", type=int, default=3, help="单片段最大尝试次数")
    parser.add_argument("--retry-backoff", type=float, default=10.0, help="重试基础退避秒数")
    parser.add_argument("--max-wait", type=int, help="单片段最长等待秒数")
    parser.add_argument("--no-reuse", action="store_true", help="关闭去重复用，强制重新生成")
    parser.add_argument("--stop-on-error", action="store_true", help="某个片段失败就停止后续")
    parser.add_argument("--reason", help="regen 时写进台账的原因说明（便于日后追溯）")
    parser.add_argument("--plan", action="store_true",
                        help="regen 时只演练，不改台账、不动文件")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    logging_util.cleanup_logs(settings.path("paths.logs"),
                              int(settings.get("logging.keep_days", 30)), prefix=LOG_NAME)
    if not args.max_wait:
        args.max_wait = int(settings.get("comfyui.job_timeout_seconds", 3600))

    print("=" * 78)
    print(f"批量生成（阶段六）  动作：{args.action}  时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 78)

    if args.action == "backfill-index":
        return cmd_backfill(args, settings, log)
    if args.action == "create":
        # create 允许不给 --project：此时从 --from-json 里的 name 取
        if not args.project and not args.from_json:
            log.error("create 需要 --project，或在 --from-json 的 JSON 里写 name")
            return 2
        return cmd_create(args, settings, log)
    if not args.project:
        log.error("这个动作需要 --project 参数")
        return 2
    if args.action == "status":
        return cmd_status(args, settings, log)
    if args.action == "reset":
        return cmd_reset(args, settings, log)
    if args.action == "regen":
        return cmd_regen(args, settings, log)
    return cmd_plan_or_run(args, settings, log, dry_run=args.action == "dry-run")


if __name__ == "__main__":
    raise SystemExit(main())
