"""【快速出片】轻量导演入口：一条命令把主题跑成"可直接出片的渲染计划"。

与严格审核版（`make_ideas → make_story → make_storyboard → make_prompts`，每步都有独立评审 + 闸门）的区别：

* **不做独立评审**（不产生 `*_review.json`），只跑**程序确定性校验**；
* 只在"影响生成/理解/一致性"的问题上做**有限返修**（默认最多 2 轮，可 `--repairs` 调）；
* 不写闸门状态（严格版的闸门不会因此被改动）；
* 复用严格版已验证的底层能力：真实六段式提示词结构（含程序生成的参考图声明行/人物锁定行）、
  `storyboard_problems` / `prompt_problems` 机械校验、时长映射规则、资产与指纹复用。

产物（同一部片目录内，文件名带 `_fast` 后缀，与严格版的产物互不干扰）::

    projects/<片名>/screenplay_fast.json / .md
    projects/<片名>/storyboard_fast.json / .md
    projects/<片名>/prompt_pack_fast.json + prompts_fast/<段 id>.txt
    projects/<片名>/render_plan_fast.json      ← 可直接 run_batch create/run
    projects/<片名>/state_fast.json            ← 本方案的运行记录（不含闸门）

用法::

    # 不花钱、不耗显卡：只拼提示词 + 跑机械校验（流程自检）
    .venv\\Scripts\\python.exe director_system\\scripts\\make_film_fast.py --film-id my-fast-01 ^
        --brief-file projects\\my-fast-01\\brief.json --dry-run

    # 真正生成（调用 DeepSeek；出图出片前会先把任务数与预估报给你）
    .venv\\Scripts\\python.exe director_system\\scripts\\make_film_fast.py --film-id my-fast-01 ^
        --brief-file projects\\my-fast-01\\brief.json --style photoreal-cinematic

断点续跑：三段是串行的，**已经写完并且通过机械校验**的那一段不会重跑（省调用），
要强制重写就加 ``--force``（旧产物照例进 ``history/``，不会被删）。
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
sys.path.insert(0, str(DIRECTOR_ROOT / "scripts"))

from video_auto import config as cfg  # noqa: E402
from video_auto import deepseek as ds  # noqa: E402

import make_ideas  # noqa: E402
import make_prompts  # noqa: E402
import make_story  # noqa: E402
import make_storyboard  # noqa: E402

FAST_SUFFIX = "_fast"
ROUTE = "fast"


#: 结构自查清单（括号成对闭合、结尾别多写 `}`、字符串用中文引号）统一收在
#: `make_ideas.FORMAT_RULES` 里，由 `call_json` **每次请求**追加——三条路线共用一份。
#: 所以本文件的三段提示词**不再自己追加**（否则同一段话会出现两遍）；
#: 报长度时把 `make_ideas.FORMAT_RULES` 算进去，才是真正发出去的请求大小。
def appended_rules_len() -> int:
    return len(make_ideas.FORMAT_RULES)


# ----------------------------------------------------------------------
# 三段提示词（复用严格版的约束文本与 schema，保证"事实同源"）
# ----------------------------------------------------------------------


def screenplay_prompt(brief: dict, conf: dict, schema: dict, style_profile: str) -> str:
    nominal, tolerance, min_seg, max_seg = make_ideas.film_scale(conf, brief)
    return f"""\
请把下面的**主题**写成一个可以拍的短片剧本。这是"快速出片"通道：**不要写创作说明、不要写多套方案**，
直接给出**一份**完整的影视化剧本。

【主题与要求（用户提供）】
{json.dumps(brief, ensure_ascii=False, indent=2)}

{make_story.story_constraints(conf, brief)}

【快速通道的额外要求】
1. 故事要有清楚的**开端 → 发展 → 转折 → 结尾**；重要信息用**画面、动作、人物反应、场景变化**表达，
   不要靠旁白解释（本片可以有人声台词，但台词要短）。
2. 场次划分按**叙事节拍**来：每场写清 `estimated_seconds`（理想秒数，可以是任意值）与
   `duration_intent`（这场为什么需要这么长）。**禁止为了凑总时长把场次切成等长的几块。**
3. 全片规模约 {nominal:g} 秒（±{tolerance:g} 秒），换算下来大约 {min_seg}~{max_seg} 段生成。
4. 风格档位：{style_profile}。
5. 台词写法（如果写对白）：`speaker` 写角色名（旁白写「旁白」），`line` 是台词本体（≤20 个汉字），
   `delivery_note` 写神态与声音特征；**台词不要写进画面描述里**（画面提示词里不会出现台词文字）。

【输出要求】
严格按下面的**字段骨架**输出**一个** JSON 对象（不要代码围栏、不要解释文字）：

{make_ideas.SCHEMA_SUMMARY_LEGEND}

```json
{make_ideas.schema_summary(schema)}
```
全部用简体中文。`total_seconds` 填**全片**时长（≈{nominal:g} 秒）。
"""


def storyboard_prompt(brief: dict, screenplay: dict, conf: dict, schema: dict) -> str:
    nominal = make_story.nominal_seconds_of(brief, conf)
    return f"""\
请把下面这份剧本拆成**可执行的分镜**（一镜＝一次生成＝一段连续镜头）。
{make_storyboard.BOARD_NO_PROMPT_RULE}

【剧本】
{json.dumps(screenplay, ensure_ascii=False, indent=2)}

{make_storyboard.storyboard_constraints(conf, nominal, brief)}

{make_storyboard.board_hard_facts(screenplay.get("continuity_bible") or {})}

【快速通道的额外要求】
1. 先说清"为什么在这里切"（`boundary_reason`）与"这一镜为什么需要这么长"（`duration_basis`），
   再把理想时长映射到合法帧数（宁长勿短）。**禁止平均分段**。
2. 运镜、景别、构图按故事需要自然给，不必堆砌术语；每镜只写看得见的动作。
3. 参考图按 7~9 张规划（角色正面/侧面、关键场景、关键道具、必要时分镜参考），
   能沿用既有资产的优先沿用（用 `asset_registry` 里的 `to_be_generated_by` 说明将来怎么出图）。

【输出要求】
严格按下面的**字段骨架**输出**一个** JSON 对象（不要代码围栏、不要解释文字）：

{make_ideas.SCHEMA_SUMMARY_LEGEND}

```json
{make_ideas.schema_summary(schema)}
```
全部用简体中文。`total_seconds` 与剧本一致。
要点：
{make_storyboard.BOARD_OUTPUT_NOTES}
{make_storyboard.FAST_BOARD_EXTRA_RULES}
"""


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pack_prompt(brief: dict, board: dict, style_prefix: str, style_profile: str,
                conf: dict, schema: dict) -> str:
    """第三段请求（提示词编译）：复用严格版的拼装函数，只把 schema 换成骨架。

    dry-run 与真跑都走这一个函数，保证"自检看到的"与"真发出去的"是同一份文字。
    """
    return make_prompts.compile_user_prompt(
        brief, board, {}, "", style_prefix, style_profile, conf, schema,
        compact_schema=True) + FAST_PACK_EXTRA_RULES


def apply_program_lines(pack: dict, board: dict, bible: dict) -> None:
    """把**由程序生成的两行**写回提示词包（与严格版同一套事实源）。

    参考图声明行与人物锁定行不靠模型写：模型写错也不该让整轮生成作废——快速通道
    在**校验之前**先把这两行派生好，于是机械校验查的是"真正会进提示词的那份文本"，
    模型只需要对分镜如实填 `reference_images`。
    """
    registry = {a["asset_id"]: a for a in board.get("asset_registry") or []}
    board_shots = {s["shot_id"]: s for s in board["shots"]}
    for shot in pack.get("shots") or []:
        lines = shot.setdefault("prompt_lines", {})
        fact = dict(shot)
        fact["characters_in_frame"] = (board_shots.get(shot["shot_id"]) or {}).get("characters_in_frame")
        lines["ref_declaration"] = make_prompts.derive_declaration(shot, registry)
        lines["character_lock"] = make_prompts.derive_character_lock(fact, registry, bible)


#: 出图批次：角色/场景/道具的**文字生成**（qwen_t2i）
MATERIALS_TARGET = "qwen_t2i"
#: 出图批次：从已有资产**派生**（qwen_edit 单图编辑 / qwen_multiref 多图参考）
DERIVE_TARGETS = ("qwen_edit", "qwen_multiref")

#: 素材批次的默认负面词（与严格版同口径：先把"文字/水印/拼图"这类通病挡住）
DEFAULT_ASSET_NEGATIVE = (
    "文字、水印、字幕、标签、签名、对话框、拼图、分格、多格、三视图、"
    "多余人物、畸形手指、多余肢体、五官扭曲、低质量、模糊、噪点、动漫、插画"
)

_RATIO_SUFFIX = {
    "16:9": " (Widescreen)", "9:16": " (Portrait Widescreen)",
    "4:3": " (Standard)", "3:4": " (Portrait Standard)", "1:1": " (Square)",
}


def asset_ratio_value(ratio: str) -> str:
    """把提示词包里写的 ``16:9`` 变成工作流节点认的写法（``16:9 (Widescreen)``）。

    取值形式来自 ``config/workflow_targets.yaml`` 里 `qwen_t2i` 的说明与前面两部片的实际用法
    （nap-01 用 `9:16 (Portrait Widescreen)`，nap-02 同）——**不是猜的**。
    """
    text = str(ratio or "").strip()
    return text + _RATIO_SUFFIX.get(text, "")


def build_asset_definitions(film_id: str, pack: dict) -> list[tuple[str, dict, str]]:
    """把提示词包的 `assets` 拆成可直接 ``run_batch create --from-json`` 的批次定义。

    返回 ``[(定义文件名, 定义内容, 一句话说明), …]``：

    * 文生图批（qwen_t2i）→ ``materials_definition_fast.json``，项目名 ``<片名>-fast-materials``；
    * 派生批（qwen_edit）→ ``derive_definition_fast.json``，项目名 ``<片名>-fast-derive``；
    * 多图参考批（qwen_multiref）→ ``boards_definition_fast.json``，项目名 ``<片名>-fast-boards``；
      `images` 按提示词包的 `derived_from` 填**来源资产**的图（顺序即 ref_image_1..N）。

    为什么由程序写（而不是像严格版那样手写）：资产表已经把所有信息（出图提示词、比例、负面词、
    派生来源）写全了，手抄一遍只会引入漂移；快速通道要"一条命令到渲染计划"，这一步必须自动。
    """
    groups: dict[str, list[dict]] = {MATERIALS_TARGET: []}
    for asset in pack.get("assets") or []:
        target = str(asset.get("qwen_target") or MATERIALS_TARGET)
        groups.setdefault(target, []).append(asset)

    plan = {
        MATERIALS_TARGET: ("materials_definition_fast.json", f"{film_id}-fast-materials",
                           "文生图（角色/场景/道具）"),
        "qwen_edit": ("derive_definition_fast.json", f"{film_id}-fast-derive", "图生图派生（qwen_edit）"),
        "qwen_multiref": ("boards_definition_fast.json", f"{film_id}-fast-boards",
                          "多图参考（qwen_multiref）"),
    }
    out: list[tuple[str, dict, str]] = []
    for target in list(plan) + [t for t in groups if t not in plan]:
        items = groups.get(target) or []
        if not items:
            continue
        file_name, project, what = plan.get(target, (f"{target}_definition_fast.json",
                                                    f"{film_id}-fast-{target}", f"出图（{target}）"))
        segments = []
        for index, asset in enumerate(items, start=1):
            extra: dict = {"negative_prompt": asset.get("negative") or DEFAULT_ASSET_NEGATIVE}
            if target == MATERIALS_TARGET:
                extra["aspect_ratio"] = asset_ratio_value(asset.get("aspect_ratio") or "16:9")
                extra["megapixels"] = 1.0
            sources = [s.strip() for s in str(asset.get("derived_from") or "").split(",") if s.strip()]
            segment: dict = {
                "id": asset["asset_id"],
                "prompt": asset["prompt"],
                "seed": 20292000 + len(out) * 100 + index,
                "extra": extra,
            }
            if target != MATERIALS_TARGET and sources:
                segment["images"] = [f"{name}.png" for name in sources]
            segments.append(segment)
        out.append((file_name, {
            "name": project, "target": target,
            "defaults": {"negative_prompt": DEFAULT_ASSET_NEGATIVE},
            "segments": segments,
        }, f"{what} {len(segments)} 条"))
    return out


def pack_problems(pack: dict, board: dict, bible: dict, conf: dict, style_prefix: str,
                  schema: dict, *, check_unused_assets: bool = True) -> list[str]:
    """提示词包的机械校验（快速通道口径）。

    先做程序该做的事（派生参考图声明行/人物锁定行），再逐项校验：
    schema → "出了图却没人用"的资产 → 提示词包自身规则。

    ``check_unused_assets=False`` 用在**复用旧产物**时：这条是后来加的"省显存"规则，
    不该让一份已经出完成片的老产物被判死而整段重跑（新生成的一定要过）。
    """
    apply_program_lines(pack, board, bible)
    return (make_ideas.schema_errors(pack, schema)
            + make_prompts.prompt_problems(
                {**pack, "_style_prefix": style_prefix}, board, conf)
            + (make_prompts.unused_asset_problems(pack) if check_unused_assets else []))


#: **只给快速出片通道**补的口径。第一条来自 2026-10-15 实测：素材的 `aspect_ratio`
#: 在 schema 里是 `minLength: 4`，"3:4" 这种三位写法会被机械校验判死（连续两次都栽在这）。
FAST_PACK_EXTRA_RULES = """

【本通道的补充口径】
* `assets[].aspect_ratio` 写"宽:高"且**至少 4 个字符**：本片统一写 `16:9`
  （与成片 1280x736 同比例）。不要写 `3:4`、`1:1` 这种三个字符的写法。
* `prompt_lines.ref_declaration`（参考图声明行）与 `character_lock`（人物锁定行）由程序按分镜
  重新派生并覆盖，你照分镜如实写即可；但 `reference_images` 的槽位、`@图片N` 编号与
  `asset_id` 必须与分镜的 `reference_assets` 逐张对上（这一条会机械校验）。
"""


def _archive(path: Path) -> None:
    if path.exists():
        history = path.parent / "history"
        history.mkdir(exist_ok=True)
        path.rename(history / f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")


def _reusable(path: Path, check) -> dict | None:
    """已有产物**通过全部机械校验**就复用它（省掉这一段 DeepSeek 调用）。

    为什么要有（项目硬性规则 8「长任务必须能断点续跑」）：三段是串行的，
    第三段失败时前两段已经写完并校验通过——重跑不该再从第一段开始烧钱。
    读不出来或校验不过就返回 None（照常重新生成），并在提示里说明原因。
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"      已有 {path.name} 读不出来（{type(exc).__name__}）→ 重新生成")
        return None
    problems = check(data)
    if problems:
        print(f"      已有 {path.name} 有 {len(problems)} 处问题 → 重新生成：{problems[0]}")
        return None
    print(f"      复用已有 {path.name}（schema 与机械校验都通过，没花调用；要重写加 --force）")
    return data


def build_render_plan(film_id: str, pack: dict, board: dict, style_profile: str) -> dict:
    """按提示词包生成渲染计划（与严格版一致的登记格式，可直接 create/run）。"""
    shots = {s["shot_id"]: s for s in board["shots"]}
    segments = []
    for shot in pack["shots"]:
        facts = shots[shot["shot_id"]]
        frames = int(facts["generation_frames"])
        segments.append({
            "id": shot["segment_id"],
            "prompt_file": f"projects/{film_id}/prompts_fast/{shot['segment_id']}.txt",
            "seed": 20292000 + len(segments) + 1,
            "duration_seconds": round(frames / 24, 4),
            "images": [f"{img['asset_id']}.png" for img in shot["reference_images"]],
            "extra": {"shot_id": shot["shot_id"], "segment_id": shot["segment_id"],
                      "generation_frames": frames, "style_profile": style_profile,
                      "reference_image_count": len(shot["reference_images"]),
                      "ref_image_size": "max", "route": ROUTE},
        })
    total = round(sum(s["duration_seconds"] for s in segments), 4)
    return {"name": film_id, "target": "t8bridge_9ref", "defaults": {"duration_seconds": 8},
            "segments": segments,
            "notes": f"快速出片通道生成的渲染计划：{len(segments)} 段，合计 {total} 秒；"
                     "登记格式与严格版一致，可 run_batch create/run；素材/分段/成片按 .gitignore 不入 Git。"}


def main() -> int:
    parser = argparse.ArgumentParser(description="快速出片：主题 → 剧本 → 分镜 → 提示词 → 渲染计划（无独立评审）")
    parser.add_argument("--film-id", required=True)
    parser.add_argument("--brief-file", required=True, help="创作简报（主题/时长/风格…）")
    parser.add_argument("--style", default="photoreal-cinematic", help="config/styles.yaml 里的风格档")
    parser.add_argument("--repairs", type=int, default=2,
                        help="schema 不合规时的返修轮数上限（快速通道默认 2）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不调用模型：只拼三段提示词 + 对已有产物跑机械校验（流程自检）")
    parser.add_argument("--force", action="store_true", help="已存在快速产物时归档旧的再重做")
    args = parser.parse_args()

    settings = cfg.load_settings()
    conf = cfg.load_director_config()
    film_dir = settings.path("paths.projects") / args.film_id
    brief_path = Path(args.brief_file)
    if not brief_path.is_absolute():
        brief_path = PROJECT_ROOT / brief_path
    if not brief_path.is_file():
        print(f"[错误] 找不到创作简报：{brief_path}")
        return 2
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    film_dir.mkdir(parents=True, exist_ok=True)
    if not (film_dir / "brief.json").exists():
        _write_json(film_dir / "brief.json", brief)

    try:
        style_profile, profile = cfg.load_style(args.style)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 读不到风格档 {args.style}：{exc}")
        return 2
    style_prefix = str(profile.get("style_prefix") or "").strip()

    screenplay_schema = make_ideas.load_schema("screenplay.schema.json")
    board_schema = make_ideas.load_schema("storyboard.schema.json")
    pack_schema = make_ideas.load_schema("prompt_pack.schema.json")
    nominal = make_story.nominal_seconds_of(brief, conf)

    print("=" * 78)
    print(f"快速出片　影片：{args.film_id}　风格档：{style_profile}　规模约 {nominal:g} 秒")
    print("=" * 78)
    print("质量控制：只跑程序确定性校验 + 有限返修（无独立评审、无闸门）")

    sp_path = film_dir / f"screenplay{FAST_SUFFIX}.json"
    board_path = film_dir / f"storyboard{FAST_SUFFIX}.json"
    pack_path = film_dir / f"prompt_pack{FAST_SUFFIX}.json"

    if args.dry_run:
        # ── 不调模型：拼提示词 + 校验已有产物（用来验证流程本身） ──
        theme_prompt = screenplay_prompt(brief, conf, screenplay_schema, style_profile)
        print(f"[1/3] 剧本提示词：{len(theme_prompt) + appended_rules_len()} 字  ✓")
        # （长度含 call_json 每次请求追加的格式规则，与真发出去的大小一致）
        if sp_path.is_file():
            sp = json.loads(sp_path.read_text(encoding="utf-8"))
            problems = (make_ideas.schema_errors(sp, screenplay_schema)
                        + make_story.duration_problems(sp, nominal, conf))
            print(f"      已有剧本校验：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
            for item in problems[:5]:
                print(f"        - {item}")
            board_prompt = storyboard_prompt(brief, sp, conf, board_schema)
            print(f"[2/3] 分镜提示词：{len(board_prompt) + appended_rules_len()} 字  ✓")
        if board_path.is_file():
            board = json.loads(board_path.read_text(encoding="utf-8"))
            sp = json.loads(sp_path.read_text(encoding="utf-8")) if sp_path.is_file() else {}
            problems = (make_ideas.schema_errors(board, board_schema)
                        + make_storyboard.storyboard_problems(
                            board, conf, nominal, sp.get("continuity_bible") or {}))
            print(f"      已有分镜校验：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
            for item in problems[:5]:
                print(f"        - {item}")
        if board_path.is_file():
            board = json.loads(board_path.read_text(encoding="utf-8"))
            pack_text = pack_prompt(brief, board, style_prefix, style_profile, conf, pack_schema)
            print(f"[3/3] 提示词编译提示词：{len(pack_text) + appended_rules_len()} 字  ✓")
            if pack_path.is_file():
                pack = json.loads(pack_path.read_text(encoding="utf-8"))
                problems = make_prompts.prompt_problems(
                    {**pack, "_style_prefix": style_prefix}, board, conf)
                problems = make_prompts.character_bible_leak_problems(
                    json.loads(sp_path.read_text(encoding="utf-8")).get("continuity_bible") or {}) + problems
                print(f"      已有提示词包校验：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
                for item in problems[:5]:
                    print(f"        - {item}")
                for file_name, definition, what in build_asset_definitions(args.film_id, pack):
                    print(f"      出图批次定义 {file_name}：{what} → 项目名 {definition['name']}")
        print()
        print("[dry-run] 没有调用模型、没有写任何文件。")
        return 0

    # ── 真正生成（三段，最多各返修 --repairs 轮） ──
    client = ds.DeepSeekClient(settings)
    calls = 0
    sp_check = lambda d: (make_ideas.schema_errors(d, screenplay_schema)  # noqa: E731
                          + make_story.duration_problems(d, nominal, conf))
    board_check = lambda d: (make_ideas.schema_errors(d, board_schema)  # noqa: E731
                             + make_storyboard.storyboard_problems(
                                 d, conf, nominal, screenplay.get("continuity_bible") or {}))

    print("[1/3] 剧本…")
    screenplay = None if args.force else _reusable(sp_path, sp_check)
    if screenplay is None:
        try:
            screenplay, notes = make_ideas.call_json(
                client, system="你是短片编剧。只输出 JSON。",
                user=screenplay_prompt(brief, conf, screenplay_schema, style_profile),
                schema=screenplay_schema, temperature=0.7, repairs=args.repairs, label="剧本",
                extra_check=lambda d: make_story.duration_problems(d, nominal, conf))
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 剧本生成失败：{exc}")
            print("       下一步：先看上面每次失败的原因——"
                  "「schema 未通过」多半是字段名/类型对不上，「不是合法 JSON」多半是括号没闭合。"
                  "要留下原文诊断就设 VIDEO_AUTO_LLM_RAW=1 再跑一次（原文落 data\\cache\\llm_raw\\）。"
                  "网络/401 一类问题查 .env 里的 DEEPSEEK_API_KEY。")
            return 1
        for note in notes:
            print(f"      {note}")
        screenplay["film_id"] = args.film_id
        if args.force:
            _archive(sp_path)
        _write_json(sp_path, screenplay)
        print(f"      已写入 {sp_path.name}（{len(screenplay.get('scenes') or [])} 场）")

    print("[2/3] 分镜…")
    board = None if args.force else _reusable(board_path, board_check)
    if board is None:
        try:
            board, notes = make_ideas.call_json(
                client, system="你是分镜师。只输出 JSON。",
                user=storyboard_prompt(brief, screenplay, conf, board_schema),
                schema=board_schema, temperature=0.6, repairs=args.repairs, label="分镜",
                extra_check=lambda d: make_storyboard.storyboard_problems(
                    d, conf, nominal, screenplay.get("continuity_bible") or {}))
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 分镜生成失败：{exc}")
            print("       下一步：剧本已通过校验并保存在 "
                  f"{sp_path.name}，直接重跑本命令即可（会自动复用它，不再花剧本那一次调用）。")
            return 1
        for note in notes:
            print(f"      {note}")
        board["film_id"] = args.film_id
        if args.force:
            _archive(board_path)
        _write_json(board_path, board)
        print(f"      已写入 {board_path.name}（{len(board.get('shots') or [])} 镜，"
              f"{len(board.get('asset_registry') or [])} 项资产）")

    print("[3/3] 六段式提示词…")
    bible = screenplay.get("continuity_bible") or {}
    pack = None
    if not args.force:
        # 复用只查"硬"问题；"出了图没人用"这类新规则只提示，不判死旧产物
        pack = _reusable(pack_path, lambda d: pack_problems(
            d, board, bible, conf, style_prefix, pack_schema, check_unused_assets=False))
    if pack is not None:
        for item in make_prompts.unused_asset_problems(pack):
            print(f"      [提示] 沿用中的旧产物有这一条：{item}（重做时才会修正）")
    if pack is None:
        try:
            pack, notes = make_ideas.call_json(
                client, system="你是 H3 提示词编译员，按真实模板输出 JSON。",
                user=pack_prompt(brief, board, style_prefix, style_profile, conf, pack_schema),
                schema=pack_schema, temperature=0.4, repairs=args.repairs, label="提示词包",
                extra_check=lambda d: pack_problems(
                    d, board, bible, conf, style_prefix, pack_schema))
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 提示词编译失败：{exc}")
            print(f"       下一步：剧本与分镜都已通过校验并保存在 {film_dir.name}，"
                  "直接重跑本命令即可（会自动复用它们，只重花这一段调用）。")
            return 1
        for note in notes:
            print(f"      {note}")
    apply_program_lines(pack, board, bible)   # 复用来的产物也重新派生一次（幂等）
    pack["film_id"] = args.film_id
    pack["style_profile"] = style_profile
    problems = (make_prompts.character_bible_leak_problems(bible)
                + pack_problems(pack, board, bible, conf, style_prefix, pack_schema))
    print(f"      机械校验：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
    for item in problems[:8]:
        print(f"        - {item}")
    if args.force:
        _archive(pack_path)
    _write_json(pack_path, pack)
    prompts_dir = film_dir / "prompts_fast"
    prompts_dir.mkdir(exist_ok=True)
    for shot in pack["shots"]:
        (prompts_dir / f"{shot['segment_id']}.txt").write_text(
            make_prompts.assemble_prompt(shot), encoding="utf-8")
    print(f"      已写入 {pack_path.name} 与 {len(pack['shots'])} 个 prompts_fast/sN.txt")

    plan = build_render_plan(args.film_id, pack, board, style_profile)
    plan_path = film_dir / f"render_plan{FAST_SUFFIX}.json"
    if args.force:
        _archive(plan_path)
    _write_json(plan_path, plan)
    # 出图批次定义（程序生成：素材批先跑，派生/多图参考批依赖素材批的产物，必须排在后面）
    asset_defs = build_asset_definitions(args.film_id, pack)
    for file_name, definition, what in asset_defs:
        def_path = film_dir / file_name
        if args.force:
            _archive(def_path)
        _write_json(def_path, definition)
        print(f"      已写入 {file_name}（{what} → 项目名 {definition['name']}）")
    _write_json(film_dir / f"state{FAST_SUFFIX}.json",
                {"route": ROUTE, "film_id": args.film_id, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "steps": ["screenplay", "storyboard", "prompts", "render_plan"],
                 "asset_definitions": [f[0] for f in asset_defs],
                 "asset_batches": [f[1]["name"] for f in asset_defs],
                 "calls": calls, "note": "快速出片通道：无独立评审、无闸门；产物带 _fast 后缀。"})

    total = sum(s["duration_seconds"] for s in plan["segments"])
    print()
    print(f"完成：{len(plan['segments'])} 段，合计 {total:.3f} 秒。DeepSeek 调用 {calls} 次。")
    print("下一步（先报预算、再出图出片）：")
    step = 1
    for file_name, definition, what in asset_defs:
        print(f"  {step}) 参考图 {what}："
              f".venv\\Scripts\\python.exe scripts\\run_batch.py create --from-json "
              f"projects\\{args.film_id}\\{file_name}　然后 run --project {definition['name']}")
        step += 1
    print(f"  {step}) 出片：.venv\\Scripts\\python.exe scripts\\run_batch.py create "
          f"--from-json projects\\{args.film_id}\\render_plan{FAST_SUFFIX}.json　然后 "
          f"run --project {args.film_id}")
    print("     （run 会在**每段提交前自动**把参考图补进 ComfyUI 的 input；"
          "想提前核对就另跑 scripts\\sync_inputs.py --project " + args.film_id + "）")
    print(f"  {step + 1}) 合成 + 交付前核对：scripts\\compose_final.py ／ "
          f"scripts\\verify_final_segments.py --project {args.film_id}")
    print(f"  {step + 2}) 收尾：scripts\\stop_comfyui.py（关掉自己拉起的 ComfyUI）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
