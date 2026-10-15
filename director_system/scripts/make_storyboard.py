"""阶段 4：分镜生成与分镜验证。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe director_system\\scripts\\make_storyboard.py --film-id animal-heads-nap-01

前置：关卡 B 必须是 APPROVED（否则直接拒绝运行）。

关键前提（来自工作流审计，不是可选项）：
**一次生成＝一段连续镜头**——一段之内不能换空间、不能切镜。
所以"镜头"与"生成段"是一一对应的：本片 3 段 = 3 个镜头，
镜头内部再分成若干"内部拍"（internal_beats），但那不是切镜。

本程序会：

1. 读已批准的剧本、上一轮评审剩下的必修项、以及你采纳的 5 条决定；
2. 调 DeepSeek 生成分镜（prompts/04_storyboard.md），用 storyboard.schema.json 校验；
3. 跑**分镜验证**：时长合计、镜头 ID 唯一、生成段一一对应、参考资产存在且不超 9 张、
   轴线同侧、镜头功能不重复、节奏有变化、本阶段不许写提示词；
4. 调 DeepSeek 做独立评审（prompts/06_quality_review.md），用 artifact_review.schema.json 校验；
5. 写 storyboard.json/.md、storyboard_review.json/.md，并把关卡 C 置为 PENDING_HUMAN_REVIEW。

不会做：写 H3 提示词（本阶段 prompt 一律为 null）、出参考图、提交 ComfyUI、修改工作流。
"""

from __future__ import annotations

import argparse
import json
import re
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_auto import config as cfg  # noqa: E402
from video_auto import deepseek as ds  # noqa: E402

import make_ideas  # noqa: E402
import make_story  # noqa: E402


# ----------------------------------------------------------------------
# 分镜验证（对应实施任务里点名的检查项）
# ----------------------------------------------------------------------

#: 这些词只出现在**早期版本**里（概念/更早的剧本），已经被批准的剧本取代。
#: 如果它们出现在分镜里，说明模型又把旧说法搬回来了 —— 也就是评审点名的
#: "两套文本并行下发"，必须打回。
#: 默认的"旧说法"禁用表（上一部片的口径）。**这部片是写死的，对新片往往是误伤**：
#: 2026-10-14 起改为**按片可覆盖**——见 :func:`superseded_terms`。默认值保留，兼容旧引用。
DEFAULT_SUPERSEDED_TERMS = ["印章", "鹰爪", "工牌照片", "镜面反光"]
SUPERSEDED_TERMS = DEFAULT_SUPERSEDED_TERMS


def superseded_terms(film_id: str | None) -> list[str]:
    """本片适用的"旧说法"禁用表。

    优先级：``projects/<film_id>/superseded_terms.json``（可为数组，或 ``{"terms": [...]}``）
    → 默认表。写空数组表示本片不设这条检查（续篇里"上一部片的旧说法"往往就是本片的正常词汇，
    例如"玻璃反光"）。
    """
    if not film_id:
        return list(DEFAULT_SUPERSEDED_TERMS)
    path = cfg.load_settings().path("paths.projects") / str(film_id) / "superseded_terms.json"
    if not path.is_file():
        return list(DEFAULT_SUPERSEDED_TERMS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return list(DEFAULT_SUPERSEDED_TERMS)
    if isinstance(data, dict):
        data = data.get("terms")
    return [str(item) for item in (data or []) if str(item).strip()]


def storyboard_problems(board: dict, conf: dict, nominal: float,
                        bible: dict | None = None) -> list[str]:
    """把'分镜验证'该查的事全查一遍，返回人类可读的问题清单。"""
    gen = conf["generation"]
    frames_cfg, step, mod = gen["frames"], gen["frames"]["step"], gen["frames"]["min_mod"]
    policy = conf.get("duration_policy") or {}
    tolerance = float(policy.get("tolerance_seconds") or 1.5)
    ideal_min = frames_cfg["min"] / gen["fps"]
    ideal_max = frames_cfg["max"] / gen["fps"]
    problems: list[str] = []

    if abs(float(board.get("total_seconds") or 0) - nominal) > tolerance:
        problems.append(
            f"total_seconds={board.get('total_seconds')} 偏离简报规模 {nominal:g} 秒超过 {tolerance:g} 秒"
        )

    shots = board.get("shots") or []
    shot_ids = [s.get("shot_id") for s in shots]
    if len(set(shot_ids)) != len(shot_ids):
        problems.append(f"镜头 ID 有重复：{shot_ids}")

    used_segments: list[str] = []
    total = 0.0
    for shot in shots:
        sid = shot.get("shot_id", "?")
        seg_id = str(shot.get("generation_segment_id") or "")
        used_segments.append(seg_id)
        # 理想时长（叙事）与映射（技术）都要有依据
        ideal = float(shot.get("ideal_edit_duration_sec") or 0)
        basis = str(shot.get("duration_basis") or "")
        boundary = str(shot.get("boundary_reason") or "")
        if ideal <= 0:
            problems.append(f"{sid}：缺少理想时长（ideal_edit_duration_sec）")
        elif ideal > ideal_max + 0.01 and not any(
            word in boundary for word in ("拆", "技术", "两段", "上限")
        ):
            problems.append(
                f"{sid}：理想时长 {ideal:g} 秒超过单段上限 {ideal_max:.2f} 秒，"
                "boundary_reason 必须说明是拆镜头还是拆生成段（技术拆分）"
            )
        elif ideal < ideal_min - 0.01 and not any(
            word in (boundary + basis) for word in ("合并", "下限", "取用", "补", "镜内")
        ):
            problems.append(
                f"{sid}：理想时长 {ideal:g} 秒低于单段下限 {ideal_min:.2f} 秒，"
                "必须在 boundary_reason／duration_basis 里写明怎么处理（合并／补拍／生成后取用）"
            )
        frames = shot.get("generation_frames")
        if not isinstance(frames, int):
            problems.append(f"{sid}：generation_frames 必须是整数")
        else:
            total += frames / gen["fps"]
            if not (frames_cfg["min"] <= frames <= frames_cfg["max"]):
                problems.append(f"{sid}：{frames} 帧超出训练区间 {frames_cfg['min']}~{frames_cfg['max']}")
            elif frames % step != mod:
                problems.append(f"{sid}：{frames} 帧不满足 {step}k+{mod}（工作流要求）")
            seconds = round(frames / gen["fps"], 2)
            if abs(float(shot.get("generation_duration_sec") or 0) - seconds) > 0.02:
                problems.append(f"{sid}：generation_duration_sec 与 {frames} 帧不符（应为 {seconds}）")
        if shot.get("prompt") is not None:
            problems.append(f"{sid}：阶段 4 不许写提示词（prompt 必须为 null，阶段 5 才编译）")
        assets = shot.get("reference_assets") or []
        slots = [a.get("slot_index") for a in assets]
        if slots != list(range(len(slots))):
            problems.append(f"{sid}：参考图槽位必须从 0 连续编号，实际 {slots}")
        if len(assets) > gen.get("ref_images_max", 9):
            problems.append(f"{sid}：参考图 {len(assets)} 张，超过上限 {gen.get('ref_images_max')}")

    if len(set(used_segments)) != len(used_segments):
        problems.append(
            f"生成段 id 有重复（一个生成段只能对应一个镜头）：{used_segments}"
        )
    # 开篇沿用既有素材的秒数（既有成片，不在 shots 表里）要计入全片总时长
    inherited = float(board.get("inherited_seconds") or 0)
    if total and abs(total + inherited - float(board.get("total_seconds") or 0)) > 0.02:
        extra = f" + 开篇沿用 {inherited:g} 秒" if inherited else ""
        problems.append(
            f"各镜帧数合计 {round(total, 2)} 秒{extra}，与 total_seconds={board.get('total_seconds')} 不一致"
        )
    # 等长规则：三段及以上且帧数完全相同，必须写出"故事为什么需要均匀节奏"
    frames_all = [int(s.get("generation_frames") or 0) for s in shots]
    if len(frames_all) >= 3 and len(set(frames_all)) == 1:
        why = board.get("equal_length_justification")
        if not why or len(str(why).strip()) < 15:
            problems.append(
                f"各镜帧数完全相同（都是 {frames_all[0]} 帧）：除非这个故事确实需要均匀节奏，"
                "否则应按戏剧节拍让镜头长短不同；确实需要就写在 equal_length_justification 里"
            )

    # 参考资产必须在资产表里登记（"角色资产存在"）
    registry = {a.get("asset_id") for a in (board.get("asset_registry") or [])}
    for shot in shots:
        for asset in shot.get("reference_assets") or []:
            if asset.get("asset_id") not in registry:
                problems.append(
                    f"{shot.get('shot_id')}：参考资产 {asset.get('asset_id')} 没有登记在 asset_registry"
                )

    # 节奏与多样性：不许每个镜头都一样
    for key, label in (("shot_function", "镜头功能"), ("camera_movement", "机位运动"),
                       ("shot_size", "景别")):
        values = [str(s.get(key)) for s in shots]
        if len(shots) > 1 and len(set(values)) == 1:
            problems.append(f"{label}不能所有镜头都一样（实际全是「{values[0]}」），节奏要有变化")
    beat_counts = [len(s.get("internal_beats") or []) for s in shots]
    if len(shots) > 1 and len(set(beat_counts)) == 1 and beat_counts[0] < 3:
        problems.append(f"每个镜头的内部拍数都一样（{beat_counts[0]}），建议按住处节奏调整")

    # 轴线：同一场景内相邻镜头换侧必须有理由
    for prev, cur in zip(shots, shots[1:]):
        if prev.get("scene_id") != cur.get("scene_id"):
            continue
        # 「不适用 → 同侧／另一侧」不是换侧：上一镜没有轴线关系（例如该场第一镜）时，
        # 这一镜建立自己的轴线，谈不上"换"。
        # 2026-10-14 修：这里原来只排除 cur＝不适用，于是 `不适用 → 同侧` 被误判成换侧，
        # 分镜连续 5 次因为这条误报被判死（原始回复落盘在 data/cache/llm_raw/ 可复核）。
        if "不适用" in (prev.get("axis_side"), cur.get("axis_side")):
            continue
        if prev.get("axis_side") != cur.get("axis_side"):
            if not cur.get("axis_change_justified"):
                problems.append(
                    f"{cur.get('shot_id')}：相对 {prev.get('shot_id')} 换了轴线一侧"
                    "（axis_side 变了）却标注 axis_change_justified=false"
                )

    if len(board.get("adopted_decisions") or []) < 5:
        problems.append("adopted_decisions 必须逐条写清已采纳的 5 条决定怎么落实")

    # ---- 事实一致性（角色表是唯一来源：剧本 continuity_bible.characters）----
    if bible is not None:
        known = {c["id"]: c for c in (bible.get("characters") or [])}
        for a in board.get("asset_registry") or []:
            cid = a.get("character_id")
            if cid and cid not in known:
                problems.append(
                    f"资产 {a.get('asset_id')} 的 character_id={cid} 在剧本角色表里不存在"
                )
        for shot in shots:
            sid = shot.get("shot_id", "?")
            in_frame = list(shot.get("characters_in_frame") or [])
            for cid in in_frame:
                if cid not in known:
                    problems.append(f"{sid}：characters_in_frame 里的 {cid} 不在剧本角色表里")
            # 画面描述里点名的角色必须登记在 characters_in_frame（否则锁行会漏人）
            text = " ".join([str(shot.get("composition", ""))]
                            + [str(x) for x in shot.get("visible_action") or []]
                            + [str(b.get("what", "")) for b in shot.get("internal_beats") or []])
            for cid, c in known.items():
                key = str(c.get("name", "")).split("（")[0].strip()
                # 画面描述里点名的方式有两种：直接写角色 id（例如 CH02b），或写角色名（例如「西装剪影」）。
                # 命中任意一种就必须登记，否则人物锁定行会漏人。
                # 注意：id 必须按"完整词"匹配——CH02 是 CH02b 的前缀，朴素包含判断会误报。
                id_hit = re.search(rf"(?<![A-Za-z0-9]){re.escape(cid)}(?![A-Za-z0-9])", text) is not None
                named = (bool(key) and key in text and not key.endswith("（一队）")) or id_hit
                if named and cid not in in_frame:
                    problems.append(
                        f"{sid}：画面描述里出现了「{key}」，但 characters_in_frame 没登记 {cid}"
                        "（人物锁定行会漏人）"
                    )
    # 不许把已被剧本取代的旧说法搬回来（评审把它当作"批准口径冲突"并拒绝裁决）
    blob = json.dumps(board, ensure_ascii=False)
    stale = [term for term in superseded_terms(board.get("film_id")) if term in blob]
    if stale:
        problems.append(
            "分镜里出现了已被剧本取代的旧说法：" + "、".join(stale)
            + "。请全部改成已批准剧本里的口径，不许两套事实并行"
            "（按片的禁用表见 projects/<片名>/superseded_terms.json）。"
        )

    # 空间平面图：三镜必须引用同一份平面，且每镜都要有机位条目
    plan_space = board.get("space_plan") or {}
    if not plan_space:
        problems.append("缺少 space_plan（三段共用的空间平面图），无法核对空间是否自相矛盾")
    else:
        if plan_space.get("shared_for_all_shots") is not True:
            problems.append("space_plan.shared_for_all_shots 必须为 true（三镜共用一份平面）")
        positioned = {c.get("shot_id") for c in plan_space.get("camera_positions") or []}
        for shot in shots:
            if shot.get("shot_id") not in positioned:
                problems.append(f"space_plan 里没有 {shot.get('shot_id')} 的机位条目")

    # 镜内分拍必须演得完：起点为 0、时间递增、不超出时长、末拍留足收尾时间
    for shot in shots:
        sid = shot.get("shot_id", "?")
        duration = float(shot.get("generation_duration_sec") or 0)
        beats = shot.get("internal_beats") or []
        times = [float(b.get("at_seconds") or 0) for b in beats]
        if times and times[0] != 0:
            problems.append(f"{sid}：internal_beats 的第一拍必须从 0 秒开始（实际 {times[0]}）")
        if times != sorted(times):
            problems.append(f"{sid}：internal_beats 的 at_seconds 必须递增，实际 {times}")
        if duration and times and times[-1] > duration:
            problems.append(f"{sid}：最后一拍在 {times[-1]} 秒，超过了本镜时长 {duration} 秒")
        if duration and times and duration - times[-1] < 1.0:
            problems.append(
                f"{sid}：最后一拍在 {times[-1]} 秒才开始，距镜尾只剩 "
                f"{round(duration - times[-1], 2)} 秒——末拍的多步动作（起身/架走/转身）演不完，"
                "请把前面的拍往前挪，给收尾留至少 1 秒"
            )
    return problems


def locked_concept(concept: dict) -> dict:
    """只把概念里"锁定不变"的部分交给模型。

    概念的文字描述（场景内容、道具、人物动作）在后续阶段已经被剧本取代；
    把整份概念原样喂进去，会造成"两套文本并行下发"——
    这是上一轮评审点名的第一条必修项的根因。所以这里只给时长分配与核心设定。
    """
    return {
        "concept_id": concept["id"],
        "title": concept["title"],
        "logline": concept["logline"],
        "pacing_intent": concept.get("pacing_intent", ""),
    }


SUPERSEDE_NOTE = """\
【文本优先级（重要）】
**已批准的剧本是唯一有效的文本**。概念里对场景、道具、人物动作的文字描述
（例如"印章""鹰爪主管""实习生照片"这类早期说法）**已经被剧本取代**，
凡是与剧本冲突的地方，一律以剧本为准，且不要在分镜里保留旧说法
——分镜只能有一套文本，不许两套并行。
概念里唯一仍然锁定的是：成片时长与每一段的帧数/秒数。

**并且：分镜文本里一处都不要出现这些旧设定**，连"某某旧设定已废弃"这类说明也不要写
（程序会按关键词检查，出现就打回）。废弃的历史由项目本身的版本归档负责，
分镜只需要干净地写当前版本：无头金属装置、红色别针、椅背织带、邻座目光。
"""


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------


def revision_board_checks(board: dict, nominal: float, conf: dict, bible: dict,
                          must_fix_count: int) -> list[str]:
    """返修分镜的机械校验：分镜规则 + 必修项逐条覆盖 + 一致性核对说明。

    与 make_story.revision_checks 同理（2026-10-15）：把内联 lambda 收成显式函数，
    参数全部显式传入，`--dry-run` 会真的调用它。
    """
    return (storyboard_problems(board, conf, nominal, bible)
            + make_story.coverage_problems(board, must_fix_count, require_consistency_note=True))


#: 阶段 4（分镜）的两条硬性口径，**严格版与快速出片通道共用同一份文字**。
#:
#: 2026-10-15 提取成常量的原因：快速出片通道为了压短提示词，把 schema 的
#: ``description`` 全文换成了"只有字段名/类型/必填"的骨架，于是**藏在 description 里的硬性规则
#: 就不再出现在提示词里**——模型连续 3 次把 ``prompt`` 写成了非 null（`prompt` 那一条说明
#: 原文是"阶段 4 一律为 null；阶段 5 编译提示词时填入"）。规则抽到这里以后，
#: 两条路线说的是同一句话，不会再各写一份而漂移。
BOARD_NO_PROMPT_RULE = "不要写 H3 提示词（`prompt` 一律 null）。"

BOARD_OUTPUT_NOTES = """\
* `shots` 的数量由**戏剧节拍与镜头边界**决定（不是由"要切成几段"决定）：
  哪里需要切、为什么在这里切，逐镜写进 `boundary_reason`；
* 每镜先写 `ideal_edit_duration_sec`（叙事理想时长，可任意秒数）与 `duration_basis`（为什么需要这么长），
  再给出映射后的 `generation_frames` 与 `generation_duration_sec`，并写 `duration_mapping_note`；
* 每镜都要有 `internal_beats`（同一镜内的分阶段，标出起止秒），并保证这些拍在
  `generation_duration_sec` 内真的演得完（安静缓慢的调性下，10 秒大约 6~8 拍是上限）；
* `reference_assets` 是本镜要提交的参考图清单（≤9 张，槽位从 0 连续编号），
  `asset_registry` 汇总全片用到的资产与"将来用什么生成"；
* 结尾要包含四段复核文字：`continuity_review`（连续性与轴线）、`pacing_review`（节奏）、
  `visual_variety_review`（视觉多样性）、`generation_feasibility_review`（生成可行性）。"""


#: **只给快速出片通道**补的两条硬性口径（严格版的提示词里逐字不动，所以放在这里单独一份）。
#: 这两条规则原文只写在 schema 的 ``description`` 里；快速通道把 description 换成了骨架，
#: 于是必须用文字把它们说回来，否则机械校验必挂（实测：``prompt`` 写成非 null 连续 2 次，
#: ``total_seconds`` 写成简报里的 60 而帧数合计 59.75 秒）。
FAST_BOARD_EXTRA_RULES = """\
* 每镜的 `prompt` 必须写 `null`（这是阶段 4，提示词要到阶段 5 才编译）。
* `total_seconds` 必须**精确等于**各镜 `generation_duration_sec` 之和（换算成秒的帧数之和，
  机械校验的容差只有 0.02 秒）——它不是"目标时长"，是长度合计；先定各镜合法帧数，再把它加起来填进去。"""


def storyboard_constraints(conf: dict, nominal: float, brief: dict | None = None) -> str:
    gen = conf["generation"]
    f = gen["frames"]
    table = make_ideas.legal_frame_table(conf)
    legal_text = "、".join(f"{s} 秒（{n} 帧）" for n, s in table)
    policy = conf.get("duration_policy") or {}
    tolerance = float(policy.get("tolerance_seconds") or 1.5)
    order = (conf.get("pacing_policy") or {}).get("order") or []
    steps = "\n".join(f"  {i}. {line}" for i, line in enumerate(order, 1))
    return f"""\
【分段的决策顺序（硬性：先叙事后技术）】
{steps}
  优先级就是：**故事需要什么镜头，导演就设计什么镜头；工作流负责尽可能实现，
  而不是让故事迁就平均时长。**

【一次生成＝一段连续镜头】
一段之内不能换空间、不能切镜、不能"走过去再进另一个房间"。
所以"镜头（shot）"与"生成段（segment）"一一对应：每个 shot 给一个
`generation_segment_id` 与一个合法帧数。镜头内部的时间变化写在 `internal_beats` 里（不是切镜）。

【合法帧数表（这是"技术可能性"，不是分配依据）】
帧数只能取 {f['step']}k+{f['min_mod']}，即 {f['min']}~{f['max']} 帧（{gen['fps']}fps）：
{legal_text}
每个镜头的 `generation_frames` 必须取上表里的值；成片时长 = 各段之和，
要落在简报规模 {nominal:g}±{tolerance:g} 秒内。

【映射规则（理想时长 → 合法帧数）】
* 理想时长落在 {table[0][1]}~{table[-1][1]} 秒之间：取**不小于**理想值的最近合法帧数（宁长勿短，
  短了会把动作切掉），差值写进 `duration_mapping_note` 作为剪辑可用余量。
* 理想时长超过 {table[-1][1]} 秒：**先改镜头设计**（拆成两个有真实剪辑点的镜头：景别/机位/视点变化），
  确需一次连续长镜头时才拆成两段生成，并把接缝风险写进 `risk_flags`。
* 理想时长不足 {table[0][1]} 秒：三选一——与相邻镜头合并为一段／生成到合法下限后剪辑取用
  （动作在该时长内完成，不算盲目裁切）／加一个镜内拍把时长填进合法区间。
* **禁止**为了对齐总时长而平均分段。三段及以上帧数完全相同，必须写 `equal_length_justification`。

【实测制作约束（硬性）】
1. 单镜 1280x736（16:9），24fps；帧数只能取 17k+5。
2. 画面内不得出现任何文字（工牌/公告栏/屏幕/时间数字/门牌一律回避）；道具靠形状与颜色成立。
3. 每镜最多 9 张参考图，且必须逐张写明用途与槽位序号；**参考图目前还不存在**，
   你只需要登记资产（asset_registry 写清是什么、将来用什么生成），不要杜撰文件名已存在。
4. 每镜画面里清晰可辨的动物头最多 2 个，其余后脑勺/虚焦/剪影。
5. 回避正面镜面与屏幕反光；精细手部特写要设计遮挡（例如藏在椅背后）。
6. 台词与旁白按剧本：分镜里只写「谁在说、嘴型与神态怎么动、声音从哪来」，**不把台词原文写进画面描述**（画面提示词里不许出现台词文字，会被烧成字幕）；环境声仍由空调低频、键盘声的"停"与"起"、椅子刮擦等承担。
7. 变头的过程要用遮挡换层表现（前景物件横过镜头），不要拍跨形态连续变形。
{make_ideas.inherited_opening_constraints(brief)}"""


def board_hard_facts(bible: dict) -> str:
    """把"反复踩的坑"写成事实清单（由程序从剧本角色表与 schema 生成，不靠模型猜）。

    2026-10-14 实测：分镜连续 4 次被机械校验打回，全部集中在同一批细节上——
    多写 schema 之外的字段（risk_facts）、机位描述只有两三个字（height_note 太短）、
    `character_id` 写成了资产名、道具的 `role` 用了枚举外的「道具参考」。
    这些都能从角色表与 schema 直接算出来；写在离模型最近的地方，比反复返修便宜得多。
    """
    characters = [c for c in (bible.get("characters") or []) if c.get("id")]
    lines = [
        "【硬事实清单（由程序从剧本角色表与 JSON Schema 生成，逐条照做）】",
        "1. `asset_registry[].character_id` 与 `shots[].characters_in_frame` 只能取下面这些 id，"
        "**逐字使用**（不要写成资产名，也不要自己另起编号）：",
    ]
    for char in characters:
        lines.append(f"   - `{char.get('id')}`：{char.get('name')}")
    lines += [
        "   场景与道具类资产**不要**填 `character_id`。",
        "2. `reference_assets[].role` 只有三个合法值：`角色参考`、`场景参考`、`分镜参考`；"
        "**道具类资产（别针、纸杯、别针盒、纸箱…）一律填 `分镜参考`**，不要写「道具参考」这类枚举外的值。",
        "3. `space_plan.camera_positions[]` 的 `where`／`height_note`／`frame_note` 都要写成**完整短句**"
        "（每个字段至少 8 个汉字，例如「平视、机位与坐姿视线同高」「低角度、桌面压住画面下缘」），"
        "不要只写「平视」「低角度」「略侧」这类两三个字的词组。",
        "4. **一个字段都不许多、一个都不许少**：只输出 schema 里列出的字段——"
        "尤其不要在 shot 里新增 `risk_facts`、`notes`、`reason` 这类 schema 之外的字段。",
        "5. `ideal_edit_duration_sec` 小于 5.17 秒或大于 15.08 秒时，必须在 `boundary_reason`／"
        "`duration_basis` 里写明怎么处理（合并／补拍／生成后取用／拆成两个有真实剪辑点的镜头）。",
        "6. `axis_side` 只有三个合法值：`同侧`、`另一侧`、`不适用`（每个场景的第一镜用 `不适用`）。"
        "**这是一条机械规则**：凡是 `axis_side` 写了 `另一侧` 的镜头，`axis_change_justified` 必须是 true，"
        "并在 `continuity_review` 里写明理由；写了 `同侧` 或 `不适用` 的，必须是 false。提交前逐镜自查一遍。",
        "7. `shot_size` 只有八个合法值：`大远景`、`远景`、`全景`、`中景`、`中近景`、`近景`、`特写`、`大特写`"
        "（不要写「略广中景」「中远景」这类枚举外的值；想表达「略广」就写 `全景` 并把机位说明放进 `camera_angle`）。",
        "8. 末拍留白：`internal_beats` 里**最后一拍的 `at_seconds` 必须 ≤ 本镜时长 − 1.0 秒**"
        "（末拍要留至少 1 秒把动作演完）；多步动作（起身／架走／转身）不要放在最后一拍。",
        "9. 顶层与子对象都只放 schema 列出的键：不要加 `shots_count_note`、`notes`、`risk_facts` 这类字段。",
        "10. `internal_beats` 的第一拍必须**从 0 秒开始**（`at_seconds` = 0），并且每一拍都写清"
        "「这一拍结束时画面是什么样」；拍与拍之间按时间顺序、不许重叠。",
    ]
    return "\n".join(lines)


def storyboard_user_prompt(
    brief: dict, concept: dict, screenplay: dict, review: dict, decisions_note: str,
    conf: dict, schema: dict,
) -> str:
    remaining = make_story.must_fix_texts(review)
    human_decisions = (review.get("recommendation") or {}).get("what_human_must_decide") or []
    bible = screenplay.get("continuity_bible") or {}
    return f"""\
请把下面这份**已批准的剧本**拆成可执行的分镜。{BOARD_NO_PROMPT_RULE}

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【已批准的概念：只作为时长分配与核心设定的依据】
{json.dumps(locked_concept(concept), ensure_ascii=False, indent=2)}

{SUPERSEDE_NOTE}

{storyboard_constraints(conf, make_story.nominal_seconds_of(brief, conf), brief)}

【已批准的剧本（第 {screenplay.get('revision', {}).get('revision', 1)} 版）】
{json.dumps(screenplay, ensure_ascii=False, indent=2)}

【用户在关卡 B 采纳的决定（必须逐条落实，并写进 adopted_decisions）】
{decisions_note}

【上一轮评审剩下的必修项（分镜必须把它们收口）】
{make_story.fixes_numbering(remaining) if remaining else "（无）"}

【评审要求由人决定、而人已经采纳的问题】
{chr(10).join(f"- {x}" for x in human_decisions) if human_decisions else "（无）"}

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文。要点：
{BOARD_OUTPUT_NOTES}
{board_hard_facts(bible)}
"""


def review_user_prompt(board: dict, conf: dict, concept: dict, schema: dict,
                       brief: dict | None = None) -> str:
    return f"""\
请以独立评审的身份审这份**分镜**（不是你自己写的，不要替它辩护）。
每条问题都要指出：哪个镜头、哪一类、多严重、证据、最小修法。

【待评审的分镜】
{json.dumps(board, ensure_ascii=False, indent=2)}

{storyboard_constraints(conf, float(board.get("total_seconds") or 0)
                        or make_story.nominal_seconds_of(brief or {}, conf), brief)}

重点检查：镜头与生成段是否真的一一对应（有没有把"走过去再进另一个房间"塞进一段）；
每镜的内部拍在给定秒数内是否演得完；轴线与空间是否连续；
关键信息是否靠画面而不是台词/文字；参考图预算（每镜 ≤9 张、同屏清晰动物头 ≤2 个）是否算得过来；
是否真的落实了用户采纳的 5 条决定；镜头功能与节奏是否有变化（不是每镜都在重复同一件事）。

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文，`target` 填 "storyboard"。`status` 必须是 PENDING_HUMAN_REVIEW，
`auto_approve` 必须是 false。
"""


def revision_user_prompt(
    brief: dict, concept: dict, old_board: dict, old_review: dict,
    must_fixes: list[str], revision: int, conf: dict, schema: dict,
) -> str:
    return f"""\
这是对**已有分镜**的修订，不是重做：按必修项逐条改，其余部分尽量逐字保留。

{make_story.PROPAGATION_RULE}

【当前分镜（第 {revision - 1} 版）】
{json.dumps(old_board, ensure_ascii=False, indent=2)}

【评审给的必修项（逐条落实，一条都不能漏）】
{make_story.fixes_numbering(must_fixes)}

【评审的完整问题清单（背景信息，属于必修项的必须改）】
{make_story.issues_text(old_review)}

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【已批准的概念：只作为时长分配与核心设定的依据】
{json.dumps(locked_concept(concept), ensure_ascii=False, indent=2)}

{SUPERSEDE_NOTE}

{storyboard_constraints(conf, make_story.nominal_seconds_of(brief, conf), brief)}

【输出要求】
严格按下面的 JSON Schema 输出**完整修订版分镜**：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文。要点：
* 三段与三个生成段的对应关系、每段帧数、成片时长**不许改**；
* 每一镜的 `internal_beats` 必须真的演得完：从 0 秒起、时间递增、
  **末拍之前要给收尾动作留至少 1 秒**；
* `space_plan` 必须是三镜共用的一份平面，并且每一镜都有对应机位条目；
* `revision` 字段必须写：`revision` = {revision}；
  `applied_fixes` 对每条必修项各写一条（以「必修N：」开头，共 {len(must_fixes)} 条），
  再加一条以「一致性核对：」开头的条目；`unchanged_note` 写保持原样的部分。
"""


# ----------------------------------------------------------------------
# 人读的 Markdown
# ----------------------------------------------------------------------


def render_storyboard_md(board: dict, meta: dict) -> str:
    out = [f"# 分镜表：{board['film_id']}", ""]
    out.append(f"- 生成时间：{meta['created_at']}　模型：{meta['model']}")
    if board.get("revision"):
        out.append(f"- 修订：第 {board['revision']['revision']} 版"
                   f"（落实 {len(board['revision']['applied_fixes'])} 条）")
    out.append(f"- 成片时长：{board['total_seconds']} 秒　镜头数：{len(board['shots'])}"
               f"　基于剧本第 {board['screenplay_revision']} 版")
    out.append("")
    out.append("> 关卡 C 状态：**PENDING_HUMAN_REVIEW**（分镜未批准不得生成渲染任务）")
    out.append("")
    if board.get("revision"):
        out.append("## 本版修订落实了什么")
        out.append("")
        for item in board["revision"]["applied_fixes"]:
            out.append(f"- {item}")
        out.append("")
        if board["revision"].get("unchanged_note"):
            out.append(f"未改动的部分：{board['revision']['unchanged_note']}")
            out.append("")
    out.append("## 你采纳的决定的落实")
    out.append("")
    for item in board["adopted_decisions"]:
        out.append(f"- {item}")
    out.append("")
    for shot in board["shots"]:
        out.append(f"## {shot['shot_id']}　{shot['scene_id']}　{shot['shot_size']}／"
                   f"{shot['camera_angle']}／{shot['camera_movement']}")
        out.append("")
        out.append(f"- 戏剧职能：{shot['dramatic_purpose']}（功能：{shot['shot_function']}）")
        out.append(f"- 生成段：{shot['generation_segment_id']}　{shot['generation_frames']} 帧 = "
                   f"{shot['generation_duration_sec']} 秒　（剪辑时长 {shot['estimated_edit_duration_sec']} 秒）")
        out.append(f"- 构图：{shot['composition']}")
        out.append(f"- 表演强度：{shot['performance_or_action_intensity']}")
        out.append(f"- 声音：{shot['sound_plan']}")
        out.append(f"- 连续性：入 {shot['continuity_in']}／出 {shot['continuity_out']}"
                   f"（轴线：{shot['axis_side']}）")
        out.append(f"- 生成策略：{shot['generation_strategy']}")
        out.append("")
        out.append("**可见动作**")
        out.append("")
        for line in shot["visible_action"]:
            out.append(f"- {line}")
        out.append("")
        out.append("**镜内分拍（同一镜，不切）**")
        out.append("")
        out.append("| 起 | 内容 |")
        out.append("| --- | --- |")
        for beat in shot["internal_beats"]:
            out.append(f"| {beat['at_seconds']}s | {beat['what']} |")
        out.append("")
        out.append(f"起状态：{shot['start_state']['character_position']}／{shot['start_state']['gaze']}／"
                   f"{shot['start_state']['prop_state']}")
        out.append("")
        out.append(f"止状态：{shot['end_state']['character_position']}／{shot['end_state']['gaze']}／"
                   f"{shot['end_state']['prop_state']}")
        out.append("")
        out.append("**参考图（槽位顺序即提交顺序）**")
        out.append("")
        out.append("| 槽 | 资产 | 用途 | 锁定什么 |")
        out.append("| --- | --- | --- | --- |")
        for asset in shot["reference_assets"]:
            out.append(f"| {asset['slot_index']} | {asset['asset_id']} | {asset['role']} | "
                       f"{asset['what_it_locks']} |")
        out.append("")
        if shot["risk_flags"]:
            out.append("风险：")
            out.append("")
            for flag in shot["risk_flags"]:
                out.append(f"- {flag}")
            out.append("")
    out.append("## 资产表（阶段 5 之前要出的图）")
    out.append("")
    out.append("| 资产 | 类型 | 说明 | 用什么生成 |")
    out.append("| --- | --- | --- | --- |")
    for asset in board["asset_registry"]:
        out.append(f"| {asset['asset_id']} | {asset['kind']} | {asset['description']} | "
                   f"{asset['to_be_generated_by']} |")
    out.append("")
    for title, key in (("连续性与轴线复核", "continuity_review"),
                       ("节奏复核", "pacing_review"),
                       ("视觉多样性复核", "visual_variety_review"),
                       ("生成可行性复核", "generation_feasibility_review")):
        out.append(f"## {title}")
        out.append("")
        out.append(board[key])
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 4：分镜生成 + 分镜验证 + 独立评审（调用 DeepSeek，产生费用）")
    parser.add_argument("--film-id", required=True)
    parser.add_argument("--force", action="store_true", help="已存在分镜时归档旧的并重新生成")
    parser.add_argument("--revise", action="store_true", help="按上一份分镜评审的必修项返修")
    parser.add_argument("--repairs", type=int, default=make_ideas.DEFAULT_REPAIRS)
    args = parser.parse_args()

    conf = cfg.load_director_config()
    settings = cfg.load_settings()
    films_root = settings.path(conf["film_layout"]["root_key"])
    film_dir = films_root / args.film_id
    if not film_dir.is_dir():
        print(f"[错误] 影片目录不存在：{film_dir}")
        return 2

    state = make_story.load_state(film_dir, conf)
    make_story.require_gate_approved(state, "A", conf)      # 真实停机点（这一组以 A 为入口）
    concept_id = str(((state.get("gates") or {}).get("A") or {}).get("chosen_concept") or "")
    decisions_note = str(((state.get("gates") or {}).get("B") or {}).get("note") or "")

    brief = json.loads((film_dir / "brief.json").read_text(encoding="utf-8"))
    ideas = json.loads((film_dir / "ideas.json").read_text(encoding="utf-8"))
    concept = make_story.find_concept(ideas, concept_id)
    screenplay = json.loads((film_dir / "screenplay.json").read_text(encoding="utf-8"))
    review = json.loads((film_dir / "screenplay_review.json").read_text(encoding="utf-8"))
    bible = (screenplay.get("continuity_bible") or {})
    nominal = make_story.nominal_seconds_of(brief, conf)

    board_path = film_dir / "storyboard.json"
    schema = make_ideas.load_schema("storyboard.schema.json")
    review_schema = make_ideas.load_schema("artifact_review.schema.json")
    review_path = film_dir / "storyboard_review.json"
    client = ds.DeepSeekClient(settings)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calls = 0
    screenplay_revision = int(screenplay.get("revision", {}).get("revision") or 1)
    board_checks = [lambda d: make_ideas.schema_errors(d, schema),
                    lambda d: storyboard_problems(d, conf, nominal, bible),
                    make_ideas.upstream_guard("screenplay_revision", screenplay_revision)]

    # 阶段复用：分镜与分镜评审各自"通过就复用"，只补缺的那一段（用户 2026-10-15 要求）。
    # 例：分镜已通过、只有评审那次失败 → 重跑本命令只重做评审，不再重写分镜。
    if not (args.force or args.revise):
        board_reused = make_ideas.reusable_artifact(board_path, board_checks, what="分镜")
        review_reused = make_ideas.reusable_artifact(
            review_path, lambda d: make_ideas.schema_errors(d, review_schema), what="分镜评审")
        if board_reused is not None and review_reused is not None:
            print()
            print("完成：本次没有调用 DeepSeek（分镜与分镜评审全部复用已有成果）。")
            print(f"      {make_ideas.convergence_verdict(review_reused, conf)}")
            print(f"请先看：{film_dir / 'storyboard.md'} ／ {film_dir / 'storyboard_review.md'}")
            print("下一步：要按评审返修加 --revise；要整段重做加 --force（旧版进 history/）。")
            return 0
        if board_reused is not None:
            board = board_reused
            print("[1/2] 分镜已有且校验通过 → 跳过生成（只补做评审）")
            print("[2/2] 独立评审分镜…")
            try:
                review2, notes2 = make_ideas.call_json(
                    client,
                    system=(DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8"),
                    user=review_user_prompt(board, conf, concept, review_schema, brief),
                    schema=review_schema,
                    temperature=0.3,
                    repairs=args.repairs,
                    label="分镜评审",
                    extra_check=make_ideas.must_fix_scope_problems,
                )
                calls += 1
            except (ds.DeepSeekError, RuntimeError) as exc:
                print(f"[错误] 分镜评审失败：{exc}")
                print("       分镜已经保存；下一步：修好原因后重跑本命令——"
                      "分镜会被自动复用，只重跑这次失败的评审（不用加 --force）。")
                return 1
            review2["film_id"] = args.film_id
            for note in notes2:
                print(f"      {note}")
            make_ideas.archive_if_exists(review_path)
            review_path.write_text(json.dumps(review2, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
            (film_dir / "storyboard_review.md").write_text(
                make_story.render_review_md(review2, {"created_at": created_at, "model": client.model}),
                encoding="utf-8",
            )
            print(f"      已写入 storyboard_review.json / .md（必修项 "
                  f"{len(review2.get('must_fix_before_next_stage') or [])} 条）")
            print(f"      {make_ideas.convergence_verdict(review2, conf)}")
            print()
            print(f"完成：本次共调用 DeepSeek {calls} 次（产生费用）。")
            return 0

    if args.revise:
        old_board = json.loads(board_path.read_text(encoding="utf-8"))
        old_review_path = film_dir / "storyboard_review.json"
        if not old_review_path.is_file():
            print("[错误] --revise 需要已有的 storyboard_review.json。")
            return 2
        old_review = json.loads(old_review_path.read_text(encoding="utf-8"))
        must_fixes = make_story.must_fix_texts(old_review)
        if not must_fixes:
            print("[停止] 上一份分镜评审没有必修项，没有可返修的内容。")
            return 2
        revision = int((old_board.get("revision") or {}).get("revision") or 1) + 1
        print(f"返修分镜：第 {revision - 1} 版 → 第 {revision} 版，{len(must_fixes)} 条必修项")
        user_prompt = revision_user_prompt(
            brief, concept, old_board, old_review, must_fixes, revision, conf, schema
        )
        extra = lambda d: revision_board_checks(  # noqa: E731
            d, nominal, conf, bible, len(must_fixes))
        label = "分镜修订"
    else:
        print(f"影片 {args.film_id}　概念 {concept_id}《{concept['title']}》"
              f"　剧本第 {screenplay.get('revision', {}).get('revision', 1)} 版"
              f"　规模约 {nominal:g} 秒（分段由戏剧节拍决定，最后映射合法帧数）")
        print(f"采纳的决定（来自关卡 B）：{decisions_note[:120]}")
        user_prompt = storyboard_user_prompt(brief, concept, screenplay, review, decisions_note, conf, schema)
        extra = lambda d: storyboard_problems(d, conf, nominal, bible)  # noqa: E731
        label = "分镜"
        revision = None

    print("[1/2] 生成分镜…")
    try:
        board, notes = make_ideas.call_json(
            client,
            system=(DIRECTOR_ROOT / "prompts" / "04_storyboard.md").read_text(encoding="utf-8"),
            user=user_prompt,
            schema=schema,
            temperature=0.5 if args.revise else 0.6,
            repairs=args.repairs,
            label=label,
            extra_check=extra,
        )
        calls += 1
    except (ds.DeepSeekError, RuntimeError) as exc:
        print(f"[错误] 分镜生成失败：{exc}")
        print("       下一步：修好原因后重跑；失败的调用同样可能计费。")
        return 1
    board["film_id"] = args.film_id
    board["concept_id"] = concept_id
    board["screenplay_revision"] = int(screenplay.get("revision", {}).get("revision") or 1)
    for note in notes:
        print(f"      {note}")
    if board_path.exists():
        make_ideas.archive_if_exists(board_path)
    board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (film_dir / "storyboard.md").write_text(
        render_storyboard_md(board, {"created_at": created_at, "model": client.model}), encoding="utf-8"
    )
    print(f"      已写入 storyboard.json / .md（{len(board['shots'])} 个镜头，"
          f"资产 {len(board['asset_registry'])} 项）")
    problems = storyboard_problems(board, conf, nominal, bible)
    print(f"      分镜验证：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
    for item in problems:
        print(f"        - {item}")

    print("[2/2] 独立评审分镜…")
    try:
        review2, notes2 = make_ideas.call_json(
            client,
            system=(DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8"),
                user=review_user_prompt(board, conf, concept, review_schema, brief),
            schema=review_schema,
            temperature=0.3,
            repairs=args.repairs,
            label="分镜评审",
            extra_check=make_ideas.must_fix_scope_problems,
        )
        calls += 1
    except (ds.DeepSeekError, RuntimeError) as exc:
        print(f"[错误] 分镜评审失败：{exc}")
        print("       分镜已经保存；下一步：修好原因后重跑（或先人工审阅分镜）。")
        return 1
    review2["film_id"] = args.film_id
    for note in notes2:
        print(f"      {note}")
    review_path = film_dir / "storyboard_review.json"
    make_ideas.archive_if_exists(review_path)
    review_path.write_text(json.dumps(review2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (film_dir / "storyboard_review.md").write_text(
        make_story.render_review_md(review2, {"created_at": created_at, "model": client.model}),
        encoding="utf-8",
    )
    print(f"      已写入 storyboard_review.json / .md（必修项 "
          f"{len(make_story.must_fix_texts(review2))} 条）")

    # 同上：本阶段属于"制作前确认"这一组，不单独设闸门。

    print()
    print(f"完成：本次共调用 DeepSeek {calls} 次（产生费用）。")
    print(f"请先看：{film_dir / 'storyboard.md'} 与 {film_dir / 'storyboard_review.md'}")
    print("关卡 C 现在是 PENDING_HUMAN_REVIEW；未批准之前不得生成渲染任务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
