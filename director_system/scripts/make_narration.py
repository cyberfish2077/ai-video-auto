"""把提示词包里的台词/旁白转成**配音台账**（TTS 用），让"台词按剧本"真正落到成片里。

为什么这样做：这个视频工作流不会"说话"，它会**把台词画成画面字幕**（负面词也拦不住）。
所以台词/旁白一律**不进画面提示词**，而是走后期配音：

    本项目 → 提示词包（shots[].dialogue）→ narration.json → TTS → 混进成片

用法::

    .venv\\Scripts\\python.exe director_system\\scripts\\make_narration.py --film-id animal-heads-nap-03
    .venv\\Scripts\\python.exe director_system\\scripts\\make_narration.py --film-id <片名> --dry-run

之后三步（都在项目根目录执行）::

    .venv\\Scripts\\python.exe scripts\\run_batch.py create --project <片名>-narration ^
        --from-json projects\\<片名>\\narration.json --force
    .venv\\Scripts\\python.exe scripts\\run_batch.py run --project <片名>-narration
    .venv\\Scripts\\python.exe scripts\\mix_narration.py --project <片名> --narration <片名>-narration ^
        --input projects\\<片名>\\final\\<片名>.mp4 --output projects\\<片名>\\final\\<片名>-带配音.mp4

规则（与 `mix_narration.py` 的配对约定一致）：

* 配音条目 id 用 ``nar-<镜头段 id>``（例如镜头段是 `s4` → `nar-s4`），mix 时按名字对上；
* 一个镜头段**一条配音**：该镜多条台词会按顺序合并成一段（同一角色连续台词没问题；
  多角色同一镜请分镜时拆开，或接受合并——合并时会在控制台给出提醒）；
* 音色来源优先级：`dialogue[].voice_note` → brief 里的 `voices[角色名]` → 默认描述。
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

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg  # noqa: E402

DEFAULT_VOICE = "自然、克制的中文播讲声线；语速中等偏慢，气息平稳，情绪内敛不煽情。"


def build_ledger(film_id: str, film_dir: Path) -> tuple[dict, list[str]]:
    """按提示词包生成配音台账；返回 (台账, 提醒清单)。"""
    pack = json.loads((film_dir / "prompt_pack.json").read_text(encoding="utf-8"))
    brief = {}
    if (film_dir / "brief.json").is_file():
        brief = json.loads((film_dir / "brief.json").read_text(encoding="utf-8"))
    voices: dict[str, str] = dict(brief.get("voices") or {})

    segments: list[dict] = []
    warnings: list[str] = []
    seed_base = 8100
    for index, shot in enumerate(pack.get("shots") or [], start=1):
        entries = shot.get("dialogue") or []
        if not entries:
            continue
        texts = [str(item.get("text") or "").strip() for item in entries]
        speakers = [str(item.get("speaker") or "").strip() for item in entries]
        texts = [t for t in texts if t]
        if not texts:
            continue
        unique_speakers = [s for s in dict.fromkeys(speakers) if s]
        if len(unique_speakers) > 1:
            warnings.append(
                f"{shot.get('shot_id')}（{shot.get('segment_id')}）有 {len(unique_speakers)} 个说话人"
                f"（{'、'.join(unique_speakers)}）——一条 TTS 只能一种音色，已按第一条的音色合并；"
                "若需要各说各的，请把这一镜拆成两段再重跑。"
            )
        voice = ""
        for item in entries:
            note = str(item.get("voice_note") or "").strip()
            if note:
                voice = note
                break
        if not voice and unique_speakers:
            voice = str(voices.get(unique_speakers[0]) or "").strip()
        if not voice:
            voice = DEFAULT_VOICE
            if unique_speakers:
                warnings.append(
                    f"{shot.get('shot_id')} 的说话人「{unique_speakers[0]}」没有音色描述："
                    "请在 brief.json 里加 `voices`（形如 {\"旁白\": \"低沉克制的男声…\"}），"
                    "或在台词条目里写 `voice_note`；现在先用默认音色。"
                )
        segments.append({
            "id": f"nar-{shot.get('segment_id')}",
            "prompt": "，".join(texts),
            "seed": seed_base + index,
            "extra": {
                "instruct": voice,
                "language": "Chinese",
                "voice_model": "1.7B",
            },
        })
    return {"name": f"{film_id}-narration", "target": "tts_voice_design",
            "defaults": {}, "segments": segments}, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="提示词包里的台词/旁白 → 配音台账（TTS）")
    parser.add_argument("--film-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="只打印会生成什么，不写文件")
    args = parser.parse_args()

    settings = cfg.load_settings()
    film_dir = settings.path("paths.projects") / args.film_id
    if not (film_dir / "prompt_pack.json").is_file():
        print(f"[错误] 找不到提示词包：{film_dir / 'prompt_pack.json'}")
        print("       下一步：先跑 director_system\\scripts\\make_prompts.py --film-id " + args.film_id)
        return 2

    ledger, warnings = build_ledger(args.film_id, film_dir)
    print(f"影片 {args.film_id}：台词/旁白 {len(ledger['segments'])} 条")
    for seg in ledger["segments"]:
        print(f"  {seg['id']:12s} {seg['prompt'][:38]}")
        print(f"               音色：{seg['extra']['instruct'][:60]}")
    for item in warnings:
        print(f"  [提醒] {item}")
    if not ledger["segments"]:
        print("  （这一部没有任何台词/旁白条目——如果剧本里写了台词，说明它们没进 `dialogue` 字段，"
              "请回分镜/提示词阶段补上）")
        return 0

    if args.dry_run:
        print("（--dry-run：没有写文件）")
        return 0
    out = film_dir / "narration.json"
    if out.exists():
        history = film_dir / "history"
        history.mkdir(exist_ok=True)
        from datetime import datetime
        out.rename(history / f"narration.{datetime.now():%Y%m%d-%H%M%S}.json")
        print(f"      旧文件已归档到 {history.name}\\")
    out.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"      已写入 {out}")
    print("      下一步：run_batch.py create --project "
          f"{args.film_id}-narration --from-json projects\\{args.film_id}\\narration.json --force")
    print(f"                run_batch.py run --project {args.film_id}-narration")
    print(f"                mix_narration.py --project {args.film_id} --narration {args.film_id}-narration "
          f"--input projects\\{args.film_id}\\final\\{args.film_id}.mp4 "
          f"--output projects\\{args.film_id}\\final\\{args.film_id}-带配音.mp4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
