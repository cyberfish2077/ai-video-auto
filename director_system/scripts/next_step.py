"""导演系统"下一步该敲什么"：读一部片子的状态，直接给出下一条命令。

用法::

    .venv\\Scripts\\python.exe director_system\\scripts\\next_step.py
    .venv\\Scripts\\python.exe director_system\\scripts\\next_step.py --film 我的片名

不带 --film 时自动挑"最近改动过、且已经写过创作简报"的那一部。
它只看状态（不调模型、不改文件），给出的命令都可以直接复制粘贴。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg  # noqa: E402

PY = r".venv\Scripts\python.exe"


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _gate(state: dict | None, gate: str) -> str:
    return str(((state or {}).get("gates") or {}).get(gate, {}).get("status") or "NOT_STARTED")


def _must_fix_count(path: Path) -> int:
    review = _read(path) or {}
    return len(review.get("must_fix_before_next_stage") or [])


def next_steps(film_dir: Path) -> list[str]:
    """返回"下一步该做什么"的清单（每条都是可直接执行的命令或动作）。"""
    name = film_dir.name
    state = _read(film_dir / "state.json")
    d = film_dir

    if _read(d / "brief.json") is None:
        return [
            f"① 写创作简报：{film_dir / 'brief.json'}（题材/片长/风格/受众/情绪/限制；可参考 "
            f"projects\\animal-heads-nap-02\\brief.json）",
            f"② 然后跑：{PY} director_system\\scripts\\make_ideas.py --film-id {name} "
            f"--brief-file projects\\{name}\\brief.json",
        ]

    gate_a, gate_b, gate_c = _gate(state, "A"), _gate(state, "B"), _gate(state, "C")

    if gate_a != "APPROVED":
        if _read(d / "ideas.json") is None:
            return [
                f"创意还没生成 → {PY} director_system\\scripts\\make_ideas.py --film-id {name} "
                f"--brief-file projects\\{name}\\brief.json",
            ]
        return [
            f"① 看 5 个概念：projects\\{name}\\ideas.md 与 idea_review.md",
            f"② 选定后批准【闸门 A】：{PY} director_system\\scripts\\review_gate.py "
            f"--film-id {name} --approve A --note \"选 cX，理由…\"",
        ]

    if gate_b != "APPROVED":
        if _read(d / "story_outline.json") is None or _read(d / "screenplay.json") is None:
            return [
                f"写大纲+剧本（含独立评审）→ {PY} director_system\\scripts\\make_story.py "
                f"--film-id {name} --concept <已批准的 cX>",
            ]
        if _read(d / "screenplay_review.json") is None:
            return [f"剧本评审缺失：重跑 make_story.py --film-id {name}（会补齐评审）"]
        fixes = _must_fix_count(d / "screenplay_review.json")
        if _read(d / "storyboard.json") is None:
            tip = f"（剧本评审还剩 {fixes} 条必修，分镜阶段会一并收口）" if fixes else ""
            return [f"拆分镜 → {PY} director_system\\scripts\\make_storyboard.py --film-id {name} {tip}".strip()]
        board_fixes = _must_fix_count(d / "storyboard_review.json")
        if _read(d / "prompt_pack.json") is None:
            tip = f"（分镜评审还剩 {board_fixes} 条必修，提示词阶段会一并收口）" if board_fixes else ""
            return [f"编译提示词包 → {PY} director_system\\scripts\\make_prompts.py --film-id {name} "
                    f"--style photoreal-cinematic {tip}".strip()]
        return [
            f"① 看剧本/分镜/提示词包：projects\\{name}\\screenplay.md、storyboard.md、prompt_pack.md",
            f"② 确认后批准【闸门 B】：{PY} director_system\\scripts\\review_gate.py "
            f"--film-id {name} --approve B --note \"同意进入批量渲染\"",
        ]

    ledger = _read(d / "segments.json")
    if ledger is None:
        return [
            f"批准了但还没建出片台账 → 先写 projects\\{name}\\render_plan.json（参考 nap-02 的写法），再：",
            f"{PY} scripts\\run_batch.py create --project {name} "
            f"--from-json projects\\{name}\\render_plan.json",
        ]
    statuses: dict[str, int] = {}
    for seg in ledger.get("segments") or []:
        statuses[str(seg.get("status"))] = statuses.get(str(seg.get("status")), 0) + 1
    todo = statuses.get("pending", 0) + statuses.get("failed", 0) + statuses.get("running", 0)
    if todo:
        return [
            f"参考图：{PY} scripts\\sync_inputs.py --project {name}",
            f"出片（{todo} 段没完成，会自动跳过已完成的、断点续跑）→ {PY} scripts\\run_batch.py run --project {name}",
            f"看状态：{PY} scripts\\run_batch.py status --project {name}",
        ]
    if not (d / "final" / f"{name}.mp4").is_file():
        return [f"合成成片 → {PY} scripts\\compose_final.py --project {name} --dissolve 0 --fade 0.25"]
    if gate_c != "APPROVED":
        return [
            f"① 交付前核对成片与台账 → {PY} scripts\\verify_final_segments.py --project {name}",
            f"② 看片：projects\\{name}\\final\\{name}.mp4",
            f"③ 通过后批准【闸门 C】：{PY} director_system\\scripts\\review_gate.py "
            f"--film-id {name} --approve C --note \"成片验收通过\"",
        ]
    return [
        f"✅ 这部片已交付：projects\\{name}\\final\\{name}.mp4",
        f"（成品与记录都不覆盖：重跑前旧版本会自动进 history\\ 或 _superseded\\）",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="导演系统：告诉我下一步该敲什么")
    parser.add_argument("--film", help="片名（默认取最近改动过、已写简报的那一部）")
    args = parser.parse_args()

    films_root = cfg.load_settings().path("paths.projects")
    if args.film:
        film_dir = films_root / args.film
        if not film_dir.is_dir():
            print(f"[错误] 没有这个片子目录：{film_dir}")
            print("       现有片子：" + "、".join(sorted(p.name for p in films_root.iterdir() if p.is_dir())))
            return 2
    else:
        candidates = [p for p in films_root.glob("*/brief.json")]
        if not candidates:
            print("[提示] 还没有任何片子的创作简报。")
            print(f"       起一部新片：先在 projects\\<新片名>\\brief.json 写创作简报，然后跑")
            print(f"       {PY} director_system\\scripts\\make_ideas.py --film-id <新片名> "
                  f"--brief-file projects\\<新片名>\\brief.json")
            return 0
        film_dir = max(candidates, key=lambda p: p.stat().st_mtime).parent

    print("=" * 78)
    print(f"导演系统 · 下一步　片子：{film_dir.name}")
    print("=" * 78)
    for index, line in enumerate(next_steps(film_dir), start=1):
        if index == 1 and not line.startswith(("①", "✅")):
            print(f"  {line}")
        else:
            print(f"  {line}")
    print()
    print("  说明：闸门（A 选案 / B 制作前确认 / C 成片质检）只能由人批准，脚本只给建议。")
    print("  两条路线怎么选、各自的入口在哪：docs\\入口指引-两条路线怎么选.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
