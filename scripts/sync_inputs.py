"""把生产目录里的素材同步到 ComfyUI 的 input 目录。

工作流的 LoadImage 只能读 ComfyUI 的 ``input`` 目录，所以生成的参考图要放进去。

    .venv\\Scripts\\python.exe scripts\\sync_inputs.py --project long120-01

规则：读取 ``projects/<项目>/project.json`` 里各段引用的图片名（没有 project.json 时
退回读台账 ``segments.json``，用于"派生/配音"这类只有台账的项目），
在素材目录里按前缀匹配（忽略 `_00001_` 这类计数后缀），
复制成工作流期望的文件名（如 ``char-linze-adult.png`` / ``board-01.png``）。

素材可能落在三个地方，都会找：``<项目>/materials/``、``<项目>-materials/materials/``、
``<项目>-derive/materials/``（图像编辑派生的角色图放这里）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg, logging_util  # noqa: E402
from video_auto import materials as materials_mod  # noqa: E402

LOG_NAME = "sync_inputs"


def find_material(materials_dir: Path, wanted: str) -> Path | None:
    """在素材目录里找与目标文件名对应的图（忽略 ComfyUI 的计数后缀）。

    实际规则在 ``video_auto.materials.find_material``（任务管理器每段提交前用的是同一份，
    两边不会再出现"手工同步能找到、自动同步找不到"这种不一致）。
    """
    return materials_mod.find_material([materials_dir], wanted)


def ledger_materials(settings, ledger_names: list[str]) -> dict[str, Path]:
    """从台账里读"片段 id -> 真实产物文件"。

    指纹命中去重时，产物会被复用、文件仍留在**原来那个项目目录**里，
    所以按目录找图会漏。这里以台账记录的 ``output_path`` 为准兜底。
    """
    found: dict[str, Path] = {}
    for name in ledger_names:
        seg_file = settings.path("paths.projects") / name / "segments.json"
        if not seg_file.exists():
            continue
        try:
            data = json.loads(seg_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for seg in data.get("segments") or []:
            paths: list[Path] = []
            if seg.get("output_path"):
                paths.append(Path(str(seg["output_path"])))
            for item in seg.get("outputs") or []:
                if isinstance(item, dict) and item.get("path"):
                    paths.append(Path(str(item["path"])))
            live = [p for p in paths if p.exists()]
            if not live:
                continue
            found[str(seg.get("id") or "")] = max(live, key=lambda p: p.stat().st_mtime)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="同步素材到 ComfyUI input 目录")
    parser.add_argument("--project", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-missing", action="store_true",
                        help="有参考图还没生成好时也继续（用于分级试拍：只跑其中几个镜头）")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    project_dir = settings.path("paths.projects") / args.project
    project_file = project_dir / "project.json"
    definition = project_file
    if not definition.exists():
        # 素材/派生/配音台账没有 project.json，只有 segments.json
        definition = project_dir / "segments.json"
    if not definition.exists():
        log.error("既没有生产定义也没有台账：%s / %s", project_file, definition)
        return 2

    project = json.loads(definition.read_text(encoding="utf-8"))
    wanted: list[str] = []
    for seg in project.get("segments", []):
        for name in seg.get("images") or []:
            if name not in wanted:
                wanted.append(str(name))
        # 首尾帧工作流用的是 first_frame / last_frame 两个字段
        for key in ("first_frame", "last_frame"):
            name = seg.get(key)
            if name and str(name) not in wanted:
                wanted.append(str(name))
    log.info("按 %s 同步 %d 张参考图", definition.name, len(wanted))

    # 素材可能在本生产目录下，也可能在配套目录里（含快速出片通道的三个批次）。
    # 目录清单与"退回主项目名"的规则收在 video_auto.materials，和任务管理器共用一份。
    base = materials_mod.base_project_name(args.project)
    candidate_dirs = materials_mod.material_dirs(settings.path("paths.projects"), args.project)
    input_dir = settings.path("comfyui.input_dir")
    # 台账兜底：产物被复用、文件留在别的项目目录时按台账里的真实路径找
    from_ledger = ledger_materials(
        settings, [args.project, f"{base}-boards", f"{base}-materials", f"{base}-derive",
                   f"{base}-fast-boards", f"{base}-fast-materials", f"{base}-fast-derive"])
    missing: list[str] = []
    copied = 0
    for name in wanted:
        source = None
        for materials_dir in candidate_dirs:
            if materials_dir.exists():
                source = find_material(materials_dir, name)
                if source is not None:
                    break
        if source is None:
            source = from_ledger.get(Path(name).stem)
        if source is None:
            missing.append(name)
            continue
        if args.dry_run:
            log.info("（dry-run）%s <- %s", name, source.name)
            continue
        shutil.copy2(source, input_dir / name)
        copied += 1
        log.info("已同步 %s <- %s", name, source.name)

    log.info("共需 %d 张，复制 %d 张，缺失 %d 张", len(wanted), copied, len(missing))
    if missing:
        message = "以下参考图还没生成好，请先跑素材任务：%s" % "、".join(missing)
        if args.allow_missing:
            log.warning("%s（--allow-missing：已继续，这些镜头现在跑会失败）", message)
        else:
            log.error("%s", message)
            log.error("命令：scripts\\run_batch.py create --from-json projects/%s/materials.json "
                      "然后 run --project %s-materials", args.project, args.project)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
