"""素材查找：把"某个项目要用的参考图"在几个可能的素材目录里找出来。

为什么单独成一个模块（2026-10-15）：同一个查找规则原来只写在 ``scripts/sync_inputs.py``
里，于是**批量出片时每段提交前不会自动补图**——多图参考批次里"后一级要读同一批刚出的图"
（例如 silhouette 要用 side）必须手工同步一次，漏了就连续 3 次
``Invalid image file`` 白跑。查找规则现在收在这里，脚本与任务管理器共用一份。

目录约定（素材可能落在这些地方，按顺序找）：::

    projects/<项目名>/materials                 （本项目的产物）
    projects/<主项目>-boards/materials
    projects/<主项目>-materials/materials
    projects/<主项目>-derive/materials
    projects/<主项目>-fast-boards/materials     （快速出片通道）
    projects/<主项目>-fast-materials/materials
    projects/<主项目>-fast-derive/materials
"""

from __future__ import annotations

from pathlib import Path

#: 这些后缀表示"配套批次"，找素材时要退回主项目名
BATCH_SUFFIXES = ("-fast-materials", "-fast-derive", "-fast-boards",
                  "-derive", "-narration", "-materials")

#: 配套批次的目录名（写全名，避免再拼错）
COMPANION_DIRS = ("boards", "materials", "derive", "fast-boards", "fast-materials", "fast-derive")


def base_project_name(project_name: str) -> str:
    """把"配套批次"的项目名退回主项目名（`X-fast-boards` → `X`）。

    注意顺序：**先长后缀后短后缀**，否则 ``-fast-materials`` 会被当成 ``-materials`` 截错。
    """
    name = str(project_name)
    for suffix in BATCH_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def material_dirs(projects_root: Path, project_name: str) -> list[Path]:
    """返回按优先级排序的素材目录列表（不保证存在，调用方自己判断）。"""
    base = base_project_name(project_name)
    root = Path(projects_root)
    return [Path(root) / project_name / "materials"] + [
        root / f"{base}-{suffix}" / "materials" for suffix in COMPANION_DIRS
    ]


def find_material(dirs: list[Path], wanted: str) -> Path | None:
    """在这些目录里找与目标文件名对应的图（忽略 ComfyUI 的 ``_00001_`` 计数后缀）。

    顺序照搬原 ``sync_inputs.py``：**按目录优先级逐个找，第一个有命中的目录说了算**；
    同一目录内先认精确文件名，否则取"同前缀里最新生成的那张"（重跑会把计数往后加）。
    保持这个顺序是有意的——严格版的老流程依赖它，不要改成"全局取最新"。
    """
    name = Path(str(wanted)).name
    stem = Path(name).stem
    for directory in dirs:
        if not directory.is_dir():
            continue
        exact = directory / name
        if exact.is_file():
            return exact
        candidates = sorted((p for p in directory.glob(f"{stem}_*.png") if p.is_file()),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    return None
