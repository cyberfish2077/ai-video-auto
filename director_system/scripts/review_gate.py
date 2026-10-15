"""人工审核关卡：查看与批准/驳回（阶段 2 起使用）。

用法（在项目根目录执行）::

    # 看某片当前四个关卡的状态
    .venv\\Scripts\\python.exe director_system\\scripts\\review_gate.py --film-id animal-heads-nap-01 --show

    # 看完提案后批准关卡 A
    .venv\\Scripts\\python.exe director_system\\scripts\\review_gate.py --film-id animal-heads-nap-01 ^
        --approve A --note "选 c3，理由：荒诞落点更干净"

    # 驳回
    .venv\\Scripts\\python.exe director_system\\scripts\\review_gate.py --film-id animal-heads-nap-01 --reject A

规则（对应 CODEX_后续实施任务.md 的全局规则 7 与 README 的 A~D）：

* 关卡只允许 ``review_gates`` 里列出的 id；
* ``APPROVED`` 只能由人在这个命令里写进去，**任何生成脚本都不许自动批准**；
* 每次改动都会追加到 ``state.json`` 的 ``history`` 里，谁在什么时候改的、为什么，都留痕；
* 需要 ``--force`` 才能覆盖一个已经是 APPROVED 的关卡（避免手滑把已批准的成果打回去）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg  # noqa: E402


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到状态文件：{path}\n"
            f"解决办法：先跑生成脚本（例如 make_ideas.py）建立 state.json，再来看/改关卡。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def show(state: dict) -> None:
    print(f"影片 {state.get('film_id')}　更新于 {state.get('updated_at')}")
    print()
    print(f"{'关卡':<4} {'名称':<16} {'状态':<20} 解锁的下一步")
    print("-" * 72)
    for gate_id, entry in (state.get("gates") or {}).items():
        print(f"{gate_id:<4} {str(entry.get('title', '')):<16} {str(entry.get('status', '')):<20} "
              f"{entry.get('unlocks', '')}")
    print()
    for gate_id, entry in (state.get("gates") or {}).items():
        if entry.get("artifacts"):
            print(f"关卡 {gate_id} 的成果：{'、'.join(entry['artifacts'])}")
        if entry.get("note"):
            print(f"关卡 {gate_id} 备注：{entry['note']}")
    history = state.get("history") or []
    if history:
        print()
        print(f"最近 {min(5, len(history))} 条改动记录：")
        for item in history[-5:]:
            print(f"  {item.get('at')}  关卡 {item.get('gate')} → {item.get('status')}"
                  f"　操作人 {item.get('by', '')}　备注：{item.get('note', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="人工审核关卡：查看 / 批准 / 驳回")
    parser.add_argument("--film-id", required=True)
    parser.add_argument("--show", action="store_true", help="只看状态，不改")
    parser.add_argument("--approve", metavar="GATE", help="批准某个关卡（例如 A）")
    parser.add_argument("--reject", metavar="GATE", help="驳回某个关卡（例如 A）")
    parser.add_argument("--note", default="", help="人的意见（会写进 state.json）")
    parser.add_argument("--by", default="", help="操作人（默认取系统用户名）")
    parser.add_argument("--force", action="store_true", help="允许改动已经是 APPROVED 的关卡")
    args = parser.parse_args()

    conf = cfg.load_director_config()
    valid_gates = {gate["id"]: gate for gate in conf["review_gates"]}
    films_root = cfg.load_settings().path(conf["film_layout"]["root_key"])
    state_path = films_root / args.film_id / str(conf["film_layout"]["state_file"])

    try:
        state = load_state(state_path)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 2

    if args.show or not (args.approve or args.reject):
        show(state)
        if not (args.approve or args.reject):
            print("（只查看，没有改动。要批准请加 --approve A，要驳回加 --reject A。）")
        return 0

    gate_id = args.approve or args.reject
    if gate_id not in valid_gates:
        print(f"[错误] 没有这个关卡：{gate_id}")
        print(f"       可用关卡：{'、'.join(valid_gates)}（定义在 director_system/config/director.yaml）")
        return 2

    gates = state.setdefault("gates", {})
    entry = gates.setdefault(gate_id, {"title": valid_gates[gate_id]["title"]})
    current = str(entry.get("status") or "NOT_STARTED")
    target = "APPROVED" if args.approve else "REJECTED"

    if current == target:
        print(f"关卡 {gate_id} 已经是 {target}，无需改动。")
        return 0
    if current == "APPROVED" and not args.force:
        print(f"[停止] 关卡 {gate_id} 已经是 APPROVED。")
        print("       下一步：确实要改就用 --force（会记进 history，留痕）。")
        return 2

    entry["status"] = target
    entry["decided_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry["decided_by"] = args.by or "human"
    if args.note:
        entry["note"] = args.note
    state["updated_at"] = entry["decided_at"]
    state.setdefault("history", []).append({
        "at": entry["decided_at"],
        "gate": gate_id,
        "from": current,
        "status": target,
        "by": entry["decided_by"],
        "note": args.note,
    })
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"关卡 {gate_id}（{valid_gates[gate_id]['title']}）：{current} → {target}")
    if target == "APPROVED":
        print(f"可以进入下一阶段：{valid_gates[gate_id]['unlocks']}")
    else:
        print("已驳回：请按备注里的意见返工，返工后再走一次本命令。")
    print(f"（记录已写入 {state_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
