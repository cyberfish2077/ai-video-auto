"""阶段 5：提示词编译（镜头提示词 + 出图提示词）。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe director_system\\scripts\\make_prompts.py ^
        --film-id animal-heads-nap-01 [--style photoreal-cinematic] [--force]

前置：关卡 C 必须是 APPROVED。

本程序会：

1. 读已批准的分镜、剧本、你采纳的决定，以及 config/styles.yaml 的风格档；
2. 调 DeepSeek 把每个镜头编译成 **T8 工作流真正吃得下的中文提示词**
   （按 references/H3提示词规范.md 第 9 节的六段结构：参考图声明／素材与音频约束／人物锁定／
   风格与全局设定／按秒时间轴／声音与负面），并同时产出资产表的出图提示词；
3. 机械校验：参考图编号与槽位一致、时间轴与镜内分拍对齐、风格行以 style_prefix 逐字开头、
   含时长与 16:9、台词文字不进画面、资产全覆盖、不许出现已废弃说法；
4. **由程序拼装**最终 .txt（`prompt_lines` 是唯一事实来源，不另写一份文本）；
5. 写 prompts/s1.txt…、prompt_pack.json/.md，并做一次独立评审。

不会做：提交 ComfyUI、生成图片、改动工作流。
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
import make_storyboard as msb  # noqa: E402

DEFAULT_STYLE = "photoreal-cinematic"


def fmt_seconds(value: float) -> str:
    """把秒数写成提示词里惯用的形式：0 / 1.2 / 10.13。"""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


# ----------------------------------------------------------------------
# 拼装与校验
# ----------------------------------------------------------------------



def derive_declaration(shot: dict, registry: dict) -> str:
    """参考图声明行：由槽位与资产表拼出来，保证与提交内容逐字对应。"""
    parts = []
    for img in shot["reference_images"]:
        entry = registry.get(img["asset_id"]) or {}
        name = str(entry.get("short_name") or img["asset_id"])
        label = "角色" if img["role"] == "角色参考" else "image"
        parts.append(f'@图片{img["slot_index"] + 1}{label}「{name}」')
    return ("、".join(parts)
            + f"。以上 {len(shot['reference_images'])} 张即本镜实际提交的全部参考图，"
              "编号与提交顺序严格一致，本镜不使用其它参考图。")


def derive_character_lock(shot: dict, registry: dict, bible: dict) -> str:
    """人物锁定行：外观与服装只从剧本角色表取一次，避免同一事实在多处被重述而漂移。"""
    chars = {c["id"]: c for c in (bible.get("characters") or [])}
    labels: dict[str, list[str]] = {}
    for img in shot["reference_images"]:
        cid = (registry.get(img["asset_id"]) or {}).get("character_id")
        if cid:
            labels.setdefault(cid, []).append(f'@图片{img["slot_index"] + 1}')
    in_frame = list(shot.get("characters_in_frame") or [])
    if not in_frame:
        return "本镜画面里不出现任何角色（纯空间或道具镜头），不得新增任何人物。"
    parts = []
    for cid in in_frame:
        c = chars.get(cid) or {}
        seen = "、".join(labels.get(cid, [])) or "（本镜未提交该角色的参考图）"
        parts.append(f'{c.get("name", cid)}（{c.get("appearance", "")}；{c.get("wardrobe", "")}，见 {seen}）')
    return ("本镜只出现 " + str(len(in_frame)) + " 名角色，颜色与造型一律不得改变、"
            "服装与配饰不得互换：" + "；".join(parts) + "。")


#: 「安全画面清单」：本工作流的负面提示词不生效（cfg=1、条件清零），所以这些硬约束
#: 必须**用正面表述**写进正文，不能只写在末行的「负面：」里。
#: 2026-10-14 提示词评审点名此条（[严重·制作风险]），因此改成程序生成、逐镜一致。
SAFE_PICTURE_LINE = (
    "画面元素清单（正面表述，逐条成立）：这是一段干净的实景画面，可见元素只有桌椅、隔板、"
    "空纸杯、别针盒、无字纸箱、散页纸张与人；画面里没有任何文字与印刷（招牌、工牌、屏幕内容、"
    "书页、标签一律不出现）；同一时刻画面里清晰的动物头只有一个（就是主角），"
    "其余人物只给侧脸、后脑勺或虚焦；玻璃、桌面与杯口只映出环境光，不映出任何人形；"
    "本镜自始至终是一段连续镜头，画面里的人不走出画框、不切镜、不换空间。"
)

#: 角色表的 appearance／wardrobe 会被**逐镜重复**进提示词（人物锁定行）。
DEFAULT_VOICE_POLICY = {"dialogue": "h3", "narration": "tts"}


def load_voice_policy(film_id: str | None, conf: dict | None = None) -> dict:
    """本片"声音由谁发"的策略：brief 覆盖 → 默认（对白由视频模型说、旁白走 TTS）。

    用户 2026-10-15 定的口径：**对白默认让视频模型直接说**（他担心 TTS 配音与口型对不上）；
    **旁白默认走 TTS**（长旁白让模型念更容易出问题），也可以在 brief 里把 narration 写成 "h3"。
    brief 写法：`"voice_policy": {"dialogue": "h3", "narration": "tts"}`
    """
    policy = dict(DEFAULT_VOICE_POLICY)
    if not film_id:
        return policy
    brief_path = cfg.load_settings().path("paths.projects") / str(film_id) / "brief.json"
    if brief_path.is_file():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            brief = {}
        for key, value in (brief.get("voice_policy") or {}).items():
            if key in policy and isinstance(value, str):
                policy[key] = value
    return policy


#: 一旦里面写了"整片演进计划"或"账本总计数"，模型就会把**后续阶段的样子**提前画出来。
#: 2026-10-15 实测事故：nap-02 的锁定行里写了"阶段一耳廓立起…阶段三完整的动物头"与
#: "别针 0→1→2→3"，结果第一镜里他就长出了兽耳、胸口还多出一枚针，整段作废重渲。
BIBLE_LEAK_PATTERNS = ["阶段零", "阶段一", "阶段二", "阶段三", "渐进动物化", "0→1", "1→2", "2→3", "第一笔", "第二笔", "第三笔"]


def character_bible_leak_problems(bible: dict) -> list[str]:
    """检查角色表里有没有"整片演进计划／账本总计数"这类会泄漏进画面的内容。

    这些内容应当写在 `continuity_bible.rules`（不进提示词正文），而不是角色的外观/服装描述里。
    """
    problems: list[str] = []
    for char in (bible or {}).get("characters") or []:
        blob = f"{char.get('appearance', '')}\n{char.get('wardrobe', '')}"
        hits = [pat for pat in BIBLE_LEAK_PATTERNS if pat in blob]
        if hits:
            problems.append(
                f"角色表 {char.get('id')} 的 appearance／wardrobe 里出现了「{'、'.join(hits)}」——"
                "这段文字会逐镜重复进提示词，模型会据此提前画出后续阶段的样子（会把片子画坏）。"
                "请把整片演进计划与账本总计数移到 continuity_bible.rules，"
                "外观/服装只写\"本镜以提交的参考图为准\"这一类不泄漏后续信息的话。"
            )
    return problems


def assemble_prompt(shot_pack: dict) -> str:
    """按六段结构拼装最终提示词文本（唯一事实来源＝prompt_lines）。"""
    lines = shot_pack["prompt_lines"]
    body = [
        lines["ref_declaration"],
        lines["material_constraint"],
        SAFE_PICTURE_LINE,
        lines["character_lock"],
        lines["style_line"],
        *lines["timeline"],
        lines["sound_line"],
        lines["negative_line"],
    ]
    return "\n".join(body) + "\n"


def prompt_problems(pack: dict, board: dict, conf: dict) -> list[str]:
    """提示词编译的机械校验（对应实施任务里点名的要求）。"""
    problems: list[str] = []
    gen = conf["generation"]
    board_shots = {s["shot_id"]: s for s in board["shots"]}
    board_assets = {a["asset_id"]: a for a in board["asset_registry"]}
    forbidden = msb.superseded_terms(board.get("film_id")) + [
        "隔断另一侧", "就在他隔壁", "桌面中央", "桌子中央"]

    if abs(float(pack.get("total_seconds") or 0) - float(board["total_seconds"])) > 0.02:
        problems.append("total_seconds 与已批准分镜不一致")

    seen_shots = set()
    for shot in pack.get("shots") or []:
        sid = shot.get("shot_id", "?")
        seen_shots.add(sid)
        src = board_shots.get(sid)
        if src is None:
            problems.append(f"{sid}：分镜里没有这个镜头")
            continue
        if shot.get("segment_id") != src["generation_segment_id"]:
            problems.append(f"{sid}：segment_id 与分镜不一致")
        if int(shot.get("frames") or 0) != int(src["generation_frames"]):
            problems.append(f"{sid}：frames 与分镜不一致（不许改时长分配）")

        images = shot.get("reference_images") or []
        labels = [img.get("label_in_prompt") for img in images]
        expected_labels = [f"@图片{i + 1}" for i in range(len(images))]
        if labels != expected_labels:
            problems.append(f"{sid}：参考图编号必须是 @图片1 起连续，实际 {labels}")
        slots = [img.get("slot_index") for img in images]
        if slots != list(range(len(slots))):
            problems.append(f"{sid}：槽位必须 0 起连续（提交顺序即语义），实际 {slots}")
        if len(images) > gen["ref_images_max"]:
            problems.append(f"{sid}：参考图超过上限 {gen['ref_images_max']}")
        # 与分镜的参考图逐张对齐（资产与顺序）
        src_assets = [a["asset_id"] for a in src["reference_assets"]]
        if [img.get("asset_id") for img in images] != src_assets:
            problems.append(
                f"{sid}：参考图与分镜不一致（分镜：{src_assets}／提示词："
                f"{[img.get('asset_id') for img in images]}）"
            )
        for img in images:
            if img.get("asset_id") not in board_assets:
                problems.append(f"{sid}：资产 {img.get('asset_id')} 未在资产表登记")

        lines = shot.get("prompt_lines") or {}
        declaration = str(lines.get("ref_declaration") or "")
        for label in expected_labels:
            if label not in declaration:
                problems.append(f"{sid}：参考图声明行里缺少 {label}")
        if f"@图片{len(images) + 1}" in declaration:
            problems.append(f"{sid}：声明了 {len(images) + 1} 号图，但本镜只提交 {len(images)} 张")

        style_line = str(lines.get("style_line") or "")
        prefix = str(pack.get("_style_prefix") or "")
        if prefix and not style_line.startswith(prefix):
            problems.append(f"{sid}：风格行没有以 config/styles.yaml 的 style_prefix 逐字开头")
        if "16:9" not in style_line:
            problems.append(f"{sid}：风格行缺少画幅 16:9")
        seconds = float(src["generation_duration_sec"])
        if not any(token in style_line for token in (fmt_seconds(seconds), f"{seconds:.2f}")):
            problems.append(f"{sid}：风格行缺少本镜时长（应写 {fmt_seconds(seconds)} 秒）")

        beats = src["internal_beats"]
        timeline = lines.get("timeline") or []
        if len(timeline) != len(beats):
            problems.append(
                f"{sid}：时间轴有 {len(timeline)} 行，但分镜的镜内分拍是 {len(beats)} 拍，必须逐拍对齐"
            )
        else:
            for index, (line, beat) in enumerate(zip(timeline, beats)):
                expected = fmt_seconds(float(beat["at_seconds"]))
                if not str(line).startswith(expected):
                    problems.append(
                        f"{sid}：第 {index + 1} 段时间轴应以「{expected}」开头（对齐分镜的镜内分拍），实际「{str(line)[:24]}」"
                    )
        if not str(lines.get("sound_line") or "").startswith("【声音】"):
            problems.append(f"{sid}：声音行必须以【声音】开头")
        if not str(lines.get("negative_line") or "").startswith("负面："):
            problems.append(f"{sid}：末行必须以「负面：」开头")
        # 台词/旁白：**允许写**（由后期配音实现），但文字一律不许进画面提示词（否则会被烧成字幕）。
        dialogue = shot.get("dialogue") or []
        declared = int(shot.get("dialogue_lines") or 0)
        if declared != len(dialogue):
            problems.append(
                f"{sid}：dialogue_lines={declared} 与 dialogue 条目数 {len(dialogue)} 对不上"
                "（条数要与条目一致，后期配音按它核对）"
            )
        body_text = json.dumps(lines, ensure_ascii=False)
        speak_by_model = load_voice_policy(board.get("film_id"), conf).get("dialogue") == "h3"
        for item in dialogue:
            line_text = str(item.get("text") or "").strip()
            if not speak_by_model and len(line_text) >= 2 and line_text in body_text:
                problems.append(
                    f"{sid}：台词/旁白原文出现在画面提示词里（「{line_text[:16]}…」）——"
                    "会被烧成画面字幕且负面词不生效。请把台词留在 `dialogue` 里，"
                    "画面只写表演、嘴型与声音来源。"
                )

        text = json.dumps(shot, ensure_ascii=False)
        stale = [term for term in forbidden if term in text]
        if stale:
            problems.append(f"{sid}：提示词里出现了已废弃或互斥说法：{'、'.join(stale)}")

    missing = set(board_shots) - seen_shots
    if missing:
        problems.append("这些镜头没有编译提示词：" + "、".join(sorted(missing)))

    compiled_assets = {a.get("asset_id") for a in pack.get("assets") or []}
    missing_assets = set(board_assets) - compiled_assets
    if missing_assets:
        problems.append("这些资产没有出图提示词：" + "、".join(sorted(missing_assets)))
    for asset in pack.get("assets") or []:
        src = board_assets.get(asset.get("asset_id"))
        if src is None:
            problems.append(f"资产 {asset.get('asset_id')} 不在分镜的资产表里")
            continue
        hint = str(src.get("to_be_generated_by") or "")
        wants_edit = "Edit" in hint or "派生" in hint
        is_edit = asset.get("qwen_target") == "qwen_edit"
        if wants_edit != is_edit:
            problems.append(
                f"资产 {asset.get('asset_id')} 的 qwen_target={asset.get('qwen_target')}"
                f" 与分镜里的生成方式不符（分镜写的是：{hint[:24]}…）"
            )
    return problems


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------


def unused_asset_problems(pack: dict) -> list[str]:
    """找出"出了图却没人用"的资产（白色资源浪费）。

    为什么要有（2026-10-15 实测）：快速通道的 `_fast_smoke` 有一条 `ast_canvas_bag`
    出了图但 8 个镜头一个都没引用它（白花一次出图）；nap-01 也有一条（`AS-PR-E`）。
    资产表是**出图清单**，多一条就多一次显存。

    "被别的资产当派生来源"也算用到了：例如"正面 → 侧面 → 剪影"这条链里，
    正面可能只出现在 `derived_from` 里而不被任何镜头直接引用。
    """
    assets = [str(a.get("asset_id")) for a in pack.get("assets") or [] if a.get("asset_id")]
    if not assets:
        return []
    used: set[str] = set()
    for shot in pack.get("shots") or []:
        for image in shot.get("reference_images") or []:
            if image.get("asset_id"):
                used.add(str(image["asset_id"]))
    for asset in pack.get("assets") or []:
        for source in str(asset.get("derived_from") or "").split(","):
            if source.strip():
                used.add(source.strip())
    unused = [name for name in assets if name not in used]
    if not unused:
        return []
    return [f"这些资产出了图却没有镜头用到（也没有被别的资产派生引用）：{'、'.join(unused)}"
            "。要么在某个镜头的 reference_images 里用上它，要么把它从 assets 里删掉（省一次出图）"]


def template_note(style_prefix: str, conf: dict) -> str:
    gen = conf["generation"]
    return f"""\
【提示词结构（照抄这个骨架，这是本工作流真正在用的中文模板）】
第 1 行：参考图声明——**由程序生成，你不要写这一行**（它会按槽位与资产表逐张拼出来，保证与提交内容一致）。
第 2 行：素材与音频约束——声明"本视频参考素材只有图片，没有音频和视频，请勿生成音频标签和视频标签"，
  并按本片规定写明台词与旁白**由后期配音呈现**（本镜台词条数填 `dialogue_lines`、条目写进 `dialogue`）、画面里不出现字幕、不需要背景音乐。
第 3 行：人物锁定——**同样由程序生成，你不要写这一行**（外观与服装只从剧本角色表取一次，避免同一事实在多处重述而漂移）。
第 4 行：风格与全局设定——**必须以下面这段 style_prefix 逐字开头**，随后接题材/场景/光线氛围，
  并写明本镜时长与画幅 16:9。
第 5 行起：按秒的时间轴——**一行一拍，行首秒数必须与分镜的镜内分拍一一对齐**
  （形如「0-1.2秒：景别+机位+可见动作」），只写看得见的画面，不写心理活动。
末两行：`【声音】…` 与 `负面：…`。

style_prefix（逐字照抄，不许改写）：{style_prefix}

【硬性约束】
1. 单次生成＝一段连续镜头：**一段之内不许换空间、不许切镜**，不许在提示词里写多个景别堆砌。
2. 画面内**绝对不出现任何文字**；道具靠形状与颜色成立。
3. 每镜清晰可辨的动物头最多 2 个（按分镜已经压到 0~1 个）。
4. **台词怎么写，取决于本片的声音策略**（见下方「本片声音策略」一行）：
   * `dialogue: h3`（默认）——**对白由视频模型直接说**：必须把每条对白写成固定格式的**对白行**放进时间轴，
     形如「角色名（神态或动作，声音特征）说：“台词”」，并写明本段只有这句是人声、不要任何字幕；
     台词仍要写进 `dialogue`（配音/核对用）。**代价**：模型有把台词烧成画面字幕的已知风险，出片后必须抽帧核验。
   * `dialogue: tts`——**台词不许进画面提示词**：画面里不出现任何台词原文（写进去会被烧成画面字幕），
     台词留在 `dialogue` 字段，由后期 TTS 配音（口径见下方策略行）。
   `narration` 同理：`tts` 时旁白不进画面；`h3` 时把旁白写成一段画外人声台词。
5. 反面词虽然不生效，仍按格式写上，保持与历史成片一致。
6. 分辨率 {gen['resolution']['width']}x{gen['resolution']['height']}，帧率 {gen['fps']}fps。
"""


def compile_user_prompt(
    brief: dict, board: dict, render_review: dict, decisions_note: str,
    style_prefix: str, profile_name: str, conf: dict, schema: dict,
    *, compact_schema: bool = False,
) -> str:
    """拼"提示词编译"的请求。

    ``compact_schema=True`` 时把 schema 换成**只有字段名/类型/必填的骨架**
    （``make_ideas.schema_summary``）——这是"快速出片"通道用的：那一段提示词里
    schema 全文占了近九成篇幅，实测让模型在尾部结构上翻车（2026-10-15）。
    机械校验仍用完整 schema，所以**产出的规格完全一样**；严格版不传这个参数，行为不变。
    """
    remaining = make_story.must_fix_texts(render_review)
    schema_block = (
        f"{make_ideas.SCHEMA_SUMMARY_LEGEND}\n\n```json\n{make_ideas.schema_summary(schema)}\n```"
        if compact_schema
        else f"```json\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n```"
    )
    schema_intro = ("严格按下面的**字段骨架**输出（键名、类型、必填标注都在里面）："
                    if compact_schema else "严格按下面的 JSON Schema 输出：")
    return f"""\
请把下面这份**已批准的分镜**编译成 T8 工作流真正吃得下的中文提示词，并同时给出资产的出图提示词。

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

{template_note(style_prefix, conf)}

【本片声音策略（照它决定台词怎么写）】
{json.dumps({**DEFAULT_VOICE_POLICY, **{k: v for k, v in ((brief.get("voice_policy") or {}) if isinstance(brief, dict) else {}).items() if k in DEFAULT_VOICE_POLICY}}, ensure_ascii=False)}
（`dialogue: h3` = 对白由视频模型直接说，台词要写成对白行进时间轴；`dialogue: tts` = 台词留在 dialogue 字段、由后期配音。
 `narration: tts` = 旁白不进画面；`narration: h3` = 旁白也写成画外人声台词。）

【风格档】config/styles.yaml 的 `{profile_name}`（`style_profile` 字段就填这个名字）

【已批准的分镜（第 {board.get('revision', {}).get('revision', 1)} 版；时长、镜内分拍、参考图槽位都以它为准）】
{json.dumps(board, ensure_ascii=False, indent=2)}

【用户在关卡 B 采纳的决定】
{decisions_note}

【分镜评审剩下的必修项（编译时一并落实）】
{make_story.fixes_numbering(remaining) if remaining else "（无）"}

【输出要求】
{schema_intro}
{schema_block}
全部用简体中文（`prompt_language` 填 "zh"）。要点：
* `shots` 必须覆盖分镜里的每一个镜头，`shot_id／segment_id／frames／seconds` 与分镜完全一致；
* `prompt_lines.timeline` 的**行数与秒数必须与分镜的 `internal_beats` 一一对齐**；
* `reference_images` 逐张给出槽位与 `@图片N` 编号，顺序与分镜的 `reference_assets` 一致；
* `assets` 必须覆盖分镜资产表里的**每一个** asset_id，并给出可直接出图的 `prompt`；
  分镜里写"从某资产派生／Qwen-Image-Edit"的，`qwen_target` 填 `qwen_edit` 且 `derived_from` 写来源资产 id；
* `compile_notes` 写清编译时做的取舍（例如为什么某镜只给一个清晰动物头）。
"""


def review_user_prompt(pack: dict, conf: dict, board: dict, schema: dict) -> str:
    return f"""\
请以独立评审的身份审这份**提示词包**（不是你自己写的，不要替它辩护）。
每条问题都要指出：哪个镜头／哪个资产、哪一类、多严重、证据、最小修法。

【待评审的提示词包】
{json.dumps(pack, ensure_ascii=False, indent=2)}

【对应的分镜（用于核对是否忠实编译）】
{json.dumps(board, ensure_ascii=False, indent=2)}

重点检查：
* 提示词是否忠实编译了分镜（时长、镜内分拍、参考图槽位与资产）；
* 是否把"一段连续镜头"写成了多空间／多景别堆砌；
* 参考图声明与人物锁定是否足以把角色锁住（本片靠参考图保持跨段一致）；
* 是否出现任何会被画成文字的表述；
* 出图提示词能否真的产出资产表要的东西（尤其"只给后脑勺""只给肩背"这类限制）。

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文，`target` 填 "prompts"。`status` 必须是 PENDING_HUMAN_REVIEW、`auto_approve` 必须是 false。
"""


def reassemble(args, settings, film_dir: Path, board: dict, conf: dict, style_prefix: str,
               pack_path: Path) -> int:
    """**不调用模型**：按现有 prompt_pack.json 重新拼装 prompts/sN.txt，并重跑机械校验。

    用途（手册 10.3 设计的路径）：评审的必修项落在提示词文本上时，
    改 `prompt_pack.json` 的 `prompt_lines`（结构化行是唯一事实来源），
    再跑一次本函数把它们落成最终提示词——既不重新编译、也不会引入新的漂移。
    程序生成的两行（参考图声明行、人物锁定行）会被重新派生，手改这两行没有意义。
    """
    if not pack_path.is_file():
        print(f"[错误] 没有 {pack_path.name}：--reassemble 用于在已有包上重新拼装，不能从零开始。")
        print("       下一步：先跑一次不带 --reassemble 的编译。")
        return 2
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["_style_prefix"] = style_prefix
    registry = {a["asset_id"]: a for a in board.get("asset_registry") or []}
    bible = json.loads((film_dir / "screenplay.json").read_text(encoding="utf-8")).get(
        "continuity_bible") or {}
    board_shots = {s["shot_id"]: s for s in board.get("shots") or []}
    for shot in pack["shots"]:
        fact_shot = dict(shot)
        fact_shot["characters_in_frame"] = (board_shots.get(shot["shot_id"]) or {}).get(
            "characters_in_frame")
        shot["prompt_lines"]["ref_declaration"] = derive_declaration(shot, registry)
        shot["prompt_lines"]["character_lock"] = derive_character_lock(fact_shot, registry, bible)
    prompts = {shot["shot_id"]: assemble_prompt(shot) for shot in pack["shots"]}
    problems = prompt_problems(pack, board, conf)
    problems = character_bible_leak_problems(bible) + problems
    print(f"重新拼装：{len(pack['shots'])} 个镜头")
    print(f"      机械校验：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
    for item in problems:
        print(f"        - {item}")
    prompts_dir = film_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for shot in pack["shots"]:
        (prompts_dir / f"{shot['segment_id']}.txt").write_text(prompts[shot["shot_id"]], encoding="utf-8")
    pack.pop("_style_prefix", None)
    pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (film_dir / "prompt_pack.md").write_text(
        render_pack_md(pack, {"created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              "model": "（重新拼装，未调用模型）"}, prompts),
        encoding="utf-8")
    print(f"      已重新写入 prompt_pack.json/.md 与 {len(pack['shots'])} 个 prompts/sN.txt")
    if args.skip_review:
        return 0 if not problems else 1
    # 手改之后重新跑一次独立评审：闸门 B 上给人看的评审必须对应当前的包，而不是改前那一版。
    review_schema = make_ideas.load_schema("artifact_review.schema.json")
    client = ds.DeepSeekClient(settings)
    print("[2/2] 独立评审提示词包（改后复核）…")
    try:
        review, notes = make_ideas.call_json(
            client,
            system=(DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8"),
            user=review_user_prompt({**pack, "_style_prefix": style_prefix}, conf, board, review_schema),
            schema=review_schema,
            temperature=0.3,
            repairs=args.repairs,
            label="提示词评审",
            extra_check=make_ideas.must_fix_scope_problems,
        )
    except (ds.DeepSeekError, RuntimeError) as exc:
        print(f"[错误] 评审失败：{exc}")
        print("       提示词包已保存；下一步：修好原因后重跑（或人工审阅提示词包）。")
        return 1
    for note in notes:
        print(f"      {note}")
    review["film_id"] = args.film_id
    review_path = film_dir / "prompt_review.json"
    make_ideas.archive_if_exists(review_path)
    review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (film_dir / "prompt_review.md").write_text(
        make_story.render_review_md(review, {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "model": client.model}),
        encoding="utf-8")
    print(f"      已写入 prompt_review.json/.md（必修项 "
          f"{len(make_story.must_fix_texts(review))} 条）")
    return 0 if not problems else 1


def render_pack_md(pack: dict, meta: dict, prompts: dict[str, str]) -> str:
    out = [f"# 提示词包：{pack['film_id']}", ""]
    out.append(f"- 编译时间：{meta['created_at']}　模型：{meta['model']}")
    if pack.get("revision"):
        out.append(f"- 修订：第 {pack['revision']['revision']} 版")
    out.append(f"- 成片时长：{pack['total_seconds']} 秒　对应分镜第 {pack['storyboard_revision']} 版"
               f"　风格档：{pack['style_profile']}")
    out.append(f"- 编译说明：{pack['compile_notes']}")
    out.append("")
    for shot in pack["shots"]:
        sid = shot["shot_id"]
        out.append(f"## {sid}（{shot['segment_id']}，{shot['frames']} 帧 = {shot['seconds']} 秒）")
        out.append("")
        out.append("**参考图（槽位顺序＝提交顺序＝提示词里的 @图片N）**")
        out.append("")
        out.append("| 槽位 | 提示词编号 | 资产 | 用途 |")
        out.append("| --- | --- | --- | --- |")
        for img in shot["reference_images"]:
            out.append(f"| {img['slot_index']} | {img['label_in_prompt']} | {img['asset_id']} | {img['role']} |")
        out.append("")
        out.append(f"**最终提示词（就是写进 prompts/{shot['segment_id']}.txt 的内容）**")
        out.append("")
        out.append("```text")
        out.append(prompts[sid].rstrip("\n"))
        out.append("```")
        out.append("")
        out.append("风险提醒：")
        out.append("")
        for note in shot["risk_notes"]:
            out.append(f"- {note}")
        out.append("")
    out.append("## 资产出图提示词（阶段 5 之后出图用）")
    out.append("")
    out.append("| 资产 | 类型 | 走哪份工作流 | 画幅 | 提示词 |")
    out.append("| --- | --- | --- | --- | --- |")
    for asset in pack["assets"]:
        prompt = asset["prompt"].replace("|", "／")
        out.append(f"| {asset['asset_id']} | {asset['kind']} | {asset['qwen_target']} | "
                   f"{asset['aspect_ratio']} | {prompt} |")
    out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 5：提示词编译（调用 DeepSeek，产生费用）")
    parser.add_argument("--film-id", required=True)
    parser.add_argument("--style", default=DEFAULT_STYLE, help="config/styles.yaml 里的风格档位名")
    parser.add_argument("--force", action="store_true", help="已存在提示词包时归档旧的并重新编译")
    parser.add_argument("--skip-review", action="store_true")
    parser.add_argument("--repairs", type=int, default=make_ideas.DEFAULT_REPAIRS)
    parser.add_argument("--reassemble", action="store_true",
                        help="不调用模型：按现有 prompt_pack.json 重新拼装 prompts/sN.txt 并重跑机械校验"
                             "（用于手改结构化行之后，把手改结果落成最终提示词）")
    args = parser.parse_args()

    conf = cfg.load_director_config()
    settings = cfg.load_settings()
    film_dir = settings.path(conf["film_layout"]["root_key"]) / args.film_id
    if not film_dir.is_dir():
        print(f"[错误] 影片目录不存在：{film_dir}")
        return 2

    state = make_story.load_state(film_dir, conf)
    make_story.require_gate_approved(state, "A", conf)      # 这一组以 A 为入口
    decisions_note = str(((state.get("gates") or {}).get("B") or {}).get("note") or "")

    brief = json.loads((film_dir / "brief.json").read_text(encoding="utf-8"))
    ideas = json.loads((film_dir / "ideas.json").read_text(encoding="utf-8"))
    board = json.loads((film_dir / "storyboard.json").read_text(encoding="utf-8"))
    render_review = json.loads((film_dir / "storyboard_review.json").read_text(encoding="utf-8"))
    concept_id = str(board.get("concept_id") or "")
    make_story.find_concept(ideas, concept_id)

    try:
        profile_name, profile = cfg.load_style(args.style)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 读不到风格档 {args.style}：{exc}")
        return 2
    style_prefix = str(profile.get("style_prefix") or "").strip()
    if not style_prefix:
        print(f"[错误] 风格档 {profile_name} 没有 style_prefix，无法编译。")
        return 2
    print(f"影片 {args.film_id}　分镜第 {board.get('revision', {}).get('revision', 1)} 版"
          f"　风格档 {profile_name}")
    print("注意：本片零配乐，因此**不使用**该档位的 bgm_line；style_prefix 逐字使用。")

    pack_path = film_dir / "prompt_pack.json"
    if args.reassemble:
        return reassemble(args, settings, film_dir, board, conf, style_prefix, pack_path)

    schema = make_ideas.load_schema("prompt_pack.schema.json")
    review_schema = make_ideas.load_schema("artifact_review.schema.json")
    client = ds.DeepSeekClient(settings)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calls = 0
    review_path = film_dir / "prompt_review.json"
    board_revision = int((board.get("revision") or {}).get("revision") or 1)

    # 阶段复用：提示词包与提示词评审各自"通过就复用"，只补缺的那一段（用户 2026-10-15 要求）。
    pack = None if args.force else make_ideas.reusable_artifact(
        pack_path,
        [lambda d: make_ideas.schema_errors(d, schema),
         lambda d: prompt_problems({**d, "_style_prefix": style_prefix}, board, conf),
         make_ideas.upstream_guard("storyboard_revision", board_revision),
         make_ideas.upstream_guard("style_profile", profile_name)],
        what="提示词包")
    review = None if (args.force or args.skip_review) else make_ideas.reusable_artifact(
        review_path, lambda d: make_ideas.schema_errors(d, review_schema), what="提示词评审")

    pack_is_new = pack is None
    if pack_is_new:
        print("[1/2] 编译提示词…")
        try:
            pack, notes = make_ideas.call_json(
                client,
                system=(DIRECTOR_ROOT / "prompts" / "05_prompt_compiler.md").read_text(encoding="utf-8"),
                user=compile_user_prompt(brief, board, render_review, decisions_note,
                                         style_prefix, profile_name, conf, schema),
                schema=schema,
                temperature=0.4,
                repairs=args.repairs,
                label="提示词编译",
                extra_check=lambda d: prompt_problems({**d, "_style_prefix": style_prefix}, board, conf),
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 编译失败：{exc}")
            print("       下一步：修好原因后重跑；失败的调用同样可能计费。")
            return 1
        for note in notes:
            print(f"      {note}")
        make_ideas.archive_if_exists(pack_path)
    else:
        print("[1/2] 提示词包已有且校验通过 → 跳过编译（只重新派生两行与 prompts/sN.txt）")
    pack["film_id"] = args.film_id
    pack["concept_id"] = concept_id
    pack["storyboard_revision"] = int((board.get("revision") or {}).get("revision") or 1)
    pack["style_profile"] = profile_name
    pack.pop("_style_prefix", None)
    for key, value in (("film_id", args.film_id), ("concept_id", concept_id),
                       ("storyboard_revision", pack["storyboard_revision"]),
                       ("style_profile", profile_name)):
        pack[key] = value

    # 声明行与锁定行由程序从事实表生成（模型不写这两行，见 schema 的说明）
    registry = {a["asset_id"]: a for a in board.get("asset_registry") or []}
    bible = json.loads((film_dir / "screenplay.json").read_text(encoding="utf-8")).get("continuity_bible") or {}
    board_shots = {s["shot_id"]: s for s in board.get("shots") or []}
    for shot in pack["shots"]:
        fact_shot = dict(shot)
        fact_shot["characters_in_frame"] = (board_shots.get(shot["shot_id"]) or {}).get("characters_in_frame")
        shot["prompt_lines"]["ref_declaration"] = derive_declaration(shot, registry)
        shot["prompt_lines"]["character_lock"] = derive_character_lock(fact_shot, registry, bible)
    prompts = {shot["shot_id"]: assemble_prompt(shot) for shot in pack["shots"]}
    problems = prompt_problems({**pack, "_style_prefix": style_prefix}, board, conf)
    problems = character_bible_leak_problems(bible) + problems
    print(f"      机械校验：{'通过' if not problems else str(len(problems)) + ' 处问题'}")
    for item in problems:
        print(f"        - {item}")
    if problems:
        print("       下一步：先解决上面的问题（可直接改 prompt_pack.json 的 prompt_lines 后重跑校验）。")

    # 落盘一律"内容不同才写"：复用路径不该去改已交付的产物（时间戳、换行都算改动）
    pack_text = json.dumps(pack, ensure_ascii=False, indent=2) + "\n"
    if not pack_path.is_file() or pack_path.read_text(encoding="utf-8") != pack_text:
        pack_path.write_text(pack_text, encoding="utf-8")
    prompts_dir = film_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for shot in pack["shots"]:
        path = prompts_dir / f"{shot['segment_id']}.txt"
        text = prompts[shot["shot_id"]]
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            if args.force:
                make_ideas.archive_if_exists(path)
            path.write_text(text, encoding="utf-8")
    pack_md = film_dir / "prompt_pack.md"
    if pack_is_new or not pack_md.is_file():
        pack_md.write_text(
            render_pack_md(pack, {"created_at": created_at, "model": client.model}, prompts),
            encoding="utf-8"
        )
    print(f"      已写入 prompt_pack.json/.md 与 {len(pack['shots'])} 个 prompts/sN.txt"
          f"（资产出图提示词 {len(pack['assets'])} 条）")
    # 只提醒、不拦：严格版有独立评审与人工闸门，多一条备用资产是可以接受的
    # （nap-01 就留了 AS-PR-E 当备用）。快速通道没有人工复核，所以那边是**硬校验**。
    for item in unused_asset_problems(pack):
        print(f"      [警告] 出图前留意：{item}")

    if review is not None:
        print(f"      复用已有 prompt_review.json（提示词评审校验通过，没花调用）")
    elif not args.skip_review:
        print("[2/2] 独立评审提示词包…")
        try:
            review, notes2 = make_ideas.call_json(
                client,
                system=(DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8"),
                user=review_user_prompt(pack, conf, board, review_schema),
                schema=review_schema,
                temperature=0.3,
                repairs=args.repairs,
                label="提示词评审",
                extra_check=make_ideas.must_fix_scope_problems,
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 评审失败：{exc}")
            print("       提示词包已保存；下一步：修好原因后重跑（或人工审阅提示词包）。")
            return 1
        review["film_id"] = args.film_id
        for note in notes2:
            print(f"      {note}")
        make_ideas.archive_if_exists(review_path)
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (film_dir / "prompt_review.md").write_text(
            make_story.render_review_md(review, {"created_at": created_at, "model": client.model}),
            encoding="utf-8",
        )
        print(f"      已写入 prompt_review.json/.md（必修项 "
              f"{len(make_story.must_fix_texts(review))} 条）")

    # 整组做完 → 把关卡 B（制作前确认）置为待人工确认
    artifact_list = ["story_outline.json", "story_outline.md", "screenplay.json", "screenplay.md",
                     "screenplay_review.json", "screenplay_review.md",
                     "storyboard.json", "storyboard.md", "storyboard_review.json", "storyboard_review.md",
                     "prompt_pack.json", "prompt_pack.md", "prompt_review.json", "prompt_review.md"]
    artifact_list += [f"prompts/{shot['segment_id']}.txt" for shot in pack["shots"]]
    make_story.record_gate(
        state, conf, "B",
        note=(f"制作前确认：剧本、分镜（{len(pack['shots'])} 镜）与提示词包（{len(pack['assets'])} 条出图提示词，"
              f"风格档 {profile_name}）都已就绪，并各自过了一遍独立评审。"
              "你批准后就进入批量渲染：先按资产表出参考图，再按 render_plan 出片。"),
        artifacts=artifact_list,
    )
    (film_dir / str(conf["film_layout"]["state_file"])).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print()
    if calls == 0:
        print("完成：本次没有调用 DeepSeek（提示词包与评审全部复用已有成果）。")
    else:
        print(f"完成：本次共调用 DeepSeek {calls} 次（产生费用）。")
    print(f"请先看：{film_dir / 'prompt_pack.md'}（含每镜最终提示词全文）")
    if not args.skip_review:
        print(f"        {film_dir / 'prompt_review.md'}")
    print("要重编译加 --force（旧包进 history/）；只改 prompt_lines 后重拼提示词用 --reassemble。")
    print("下一步：按 prompt_pack.json 的 assets 出参考图（需要启动 ComfyUI；这一步要你先确认）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
