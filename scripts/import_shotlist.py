"""把外部镜头清单导入成生产项目（阶段十，《剪枝者》用）。

内容已经由外部写好（不是 DeepSeek 生成的），本脚本只做"翻译"：
镜头清单 + 资产登记表 → 每镜 H3 提示词 + 四份台账（视频/素材/派生/配音）。

用法（在项目根目录执行）::

    # 全量导入 72 镜头
    .venv\\Scripts\\python.exe scripts\\import_shotlist.py --name jianzhizhe

    # 先只导入一两个镜头做小样（沿用同一套规则，产物落到另一个项目名）
    .venv\\Scripts\\python.exe scripts\\import_shotlist.py --name jianzhizhe-smoke --only 02 --chain none

    # 只看会写出什么，不落盘
    .venv\\Scripts\\python.exe scripts\\import_shotlist.py --name jianzhizhe --dry-run
"""

from __future__ import annotations

import argparse
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

from video_auto import config as cfg  # noqa: E402
from video_auto import shotlist as sl  # noqa: E402


def _pick_single(directory: Path, pattern: str, what: str) -> Path:
    """在一个目录里按通配找唯一文件；找不到或有多个就明确报错。"""
    hits = sorted(directory.glob(pattern))
    if not hits:
        raise sl.ShotlistError(f"{directory} 里找不到{what}（通配 {pattern}）。")
    if len(hits) > 1:
        names = "、".join(p.name for p in hits)
        raise sl.ShotlistError(
            f"{directory} 里有多个{what}：{names}\n解决办法：用命令行参数明确指定其中一个。"
        )
    return hits[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="把外部镜头清单导入成生产项目")
    parser.add_argument("--name", required=True, help="项目名（产物目录 projects/<名字>/）")
    parser.add_argument("--shotlist", help="镜头清单 JSON（默认在项目目录里找 *机器可读清单*.json）")
    parser.add_argument("--script", help="人读剧本 txt（取说话人，默认在项目目录里找 *剧本*.txt）")
    parser.add_argument("--assets", help="资产登记表（默认 projects/<项目名>/assets.yaml）")
    parser.add_argument("--style", default="photoreal-cinematic", help="config/styles.yaml 里的风格档")
    parser.add_argument("--aspect", default="16:9 横画幅（1280x736）", help="提示词里的画幅表述")
    parser.add_argument("--only", help="只导入这些镜头，逗号分隔，例如 02,09,14")
    parser.add_argument("--chain", choices=["same-env", "all", "none"], default="same-env",
                        help="连续性锚点：同一场景才挂上一镜头尾帧（默认）/全部挂/不挂")
    parser.add_argument("--voice-mode", choices=["dubbed", "h3"], default="dubbed",
                        help="dubbed=人声全部后期配音（默认）；h3=允许视频模型读台词")
    parser.add_argument("--seed", type=int, default=20270101, help="种子基数（每镜 +1）")
    parser.add_argument("--force", action="store_true", help="目标目录已有 project.json 时覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    args = parser.parse_args()

    settings = cfg.load_settings()
    projects_dir = settings.path("paths.projects")
    project_dir = projects_dir / args.name

    style_name, style = cfg.load_style(args.style)
    try:
        shotlist_path = Path(args.shotlist) if args.shotlist else _pick_single(
            project_dir, "*机器可读清单*.json", "镜头清单")
        script_path = Path(args.script) if args.script else _pick_single(
            project_dir, "*剧本*.txt", "剧本 txt")
        assets_path = Path(args.assets) if args.assets else project_dir / "assets.yaml"

        assets = sl.load_assets(assets_path)
        meta, shots = sl.load_shotlist(shotlist_path)
        speakers, lines = sl.load_script_speakers(script_path)
    except sl.ShotlistError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    # 把从 txt 里读到的说话人与台词补进镜头
    filled = sl.attach_script_speakers(shots, speakers, lines)
    print(f"说话人：从剧本 txt 读到 {len(speakers)} 条，已挂到 {filled} 个镜头上")

    if args.only:
        wanted = {x.strip().zfill(2) for x in args.only.split(",") if x.strip()}
        picked = [s for s in shots if s.shot_id in wanted]
        missing = sorted(wanted - {s.shot_id for s in picked})
        if missing:
            print(f"[错误] --only 里的这些镜头在清单里不存在：{'、'.join(missing)}", file=sys.stderr)
            return 2
        shots = picked

    existing = project_dir / "project.json"
    if existing.exists() and not args.force and not args.dry_run:
        print(f"[错误] {existing} 已存在。要重建请加 --force（旧台账会被覆盖）。", file=sys.stderr)
        return 2

    total = sum(s.duration for s in shots)
    print(f"项目：{args.name}　目录：{project_dir}")
    print(f"风格档：{style_name}（{style.get('label', '')}）")
    print(f"导入镜头：{len(shots)} 个，合计 {total:.0f} 秒（{total / 60:.2f} 分钟）")
    print(f"连续性锚点：{args.chain}　人声模式：{args.voice_mode}　种子基数：{args.seed}")
    if args.dry_run:
        for shot in shots:
            images, _decls, _used = sl.shot_assets(
                shot, assets["characters"], assets["environments"])
            print(f"  {shot.seg_id}  {shot.duration:.0f}s  {shot.title}  参考图 {' → '.join(images)}")
        print("（--dry-run：没有写任何文件）")
        return 0

    written = sl.write_production(
        project_dir,
        name=args.name,
        meta=meta,
        shots=shots,
        assets=assets,
        style_prefix=str(style.get("style_prefix") or ""),
        aspect=args.aspect,
        negative=str(style.get("negative") or ""),
        material_style=str(assets.get("material_style") or "") or str(style.get("material_style") or ""),
        base_seed=args.seed,
        chain_mode=args.chain,
        voice_mode=args.voice_mode,
    )
    print()
    for key in ("project", "materials", "boards", "derive", "narration", "shotlist"):
        path = written.get(key)
        if path:
            print(f"  已写出 {key:10s} {path.relative_to(PROJECT_ROOT)}")
    print("  生成顺序（有依赖）：materials → derive → sync_inputs → boards → sync_inputs → 视频")
    prompts = sorted(p for k, p in written.items() if k.startswith("prompt_"))
    print(f"  已写出提示词 {len(prompts)} 份 -> {prompts[0].relative_to(PROJECT_ROOT)} … "
          f"{prompts[-1].relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
