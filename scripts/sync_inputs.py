"""把生产目录里的素材同步到 ComfyUI 的 input 目录。

工作流的 LoadImage 只能读 ComfyUI 的 ``input`` 目录，所以生成的参考图要放进去。

    .venv\\Scripts\\python.exe scripts\\sync_inputs.py --project long120-01

规则：读取 ``projects/<项目>/project.json`` 里各段引用的图片名，
在 ``projects/<项目>/materials/`` 里按前缀匹配（忽略 `_00001_` 这类计数后缀），
复制成工作流期望的文件名（如 ``char-1.png`` / ``board-01.png``）。
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

LOG_NAME = "sync_inputs"


def find_material(materials_dir: Path, wanted: str) -> Path | None:
    """在素材目录里找与目标文件名对应的图（忽略 ComfyUI 的计数后缀）。"""
    exact = materials_dir / wanted
    if exact.exists():
        return exact
    stem = Path(wanted).stem
    candidates = sorted(
        (p for p in materials_dir.glob(f"{stem}_*.png")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description="同步素材到 ComfyUI input 目录")
    parser.add_argument("--project", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = cfg.load_settings()
    log = logging_util.setup_logging(settings.path("paths.logs"), LOG_NAME,
                                     level=str(settings.get("logging.level", "INFO")))
    project_dir = settings.path("paths.projects") / args.project
    project_file = project_dir / "project.json"
    if not project_file.exists():
        log.error("找不到生产定义：%s", project_file)
        return 2

    project = json.loads(project_file.read_text(encoding="utf-8"))
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

    # 素材可能在本生产目录下，也可能在配套的 <项目名>-materials 目录下
    candidate_dirs = [
        project_dir / "materials",
        settings.path("paths.projects") / f"{args.project}-materials" / "materials",
    ]
    input_dir = settings.path("comfyui.input_dir")
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
        log.error("以下参考图还没生成好，请先跑素材任务：%s", "、".join(missing))
        log.error("命令：scripts\\run_batch.py create --from-json projects/%s/materials.json "
                  "然后 run --project %s-materials", args.project, args.project)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
