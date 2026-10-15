"""剧本生成与生产目录装配（阶段八第一步）。

分工设计（关键）：

* **DeepSeek 只负责内容**：剧情概要、角色外观、每段的镜头描述与台词、音效、分镜图提示词；
* **格式由本模块组装**：H3 中文时间轴模板（含禁字幕条款、跨段连续性说明、负面词）
  全部由代码按固定模板拼出来，模型不参与格式 —— 这样格式永远合规，不会因为模型发挥而跑偏。

产出（全部落在 ``projects/<项目名>/``）：

* ``script.md``     剧本（给人看的）
* ``prompts/seg-XX.txt``  每段的 H3 提示词（给工作流用）
* ``project.json``  生产定义（分段、种子、参考图清单）
* ``materials.json`` 素材生成任务（角色三视图 + 各段分镜图，交给 qwen_t2i）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 提示词里的固定约束行（逐字写死，不允许改）
TEXT_BAN_LINE = (
    "【硬性要求】画面中**不要出现任何对话或旁白的字幕文字**（台词字幕、旁白字幕、片头字、标题、贴纸、"
    "水印、界面文字）；对白只以人声呈现，不要以字幕形式显示。"
    "场景里本来就有的牌匾、楹联、招牌、幡旗上的文字属于场景元素，**应当保留、不要抹掉**。"
    "No dialogue subtitles, no captions, no watermark."
)
ASSET_RULE_LINE = (
    "本视频参考素材只有图片，没有音频和视频，请勿生成音频标签和视频标签，"
    "此视频只保留必要的音效与对白台词"
)
#: 统一背景音乐：让各段音乐风格/速度一致，配合合成时的交叉溶解，接缝处才不会"断掉"
BGM_RULE_LINE = (
    "本片全程有统一的背景音乐，各段必须保持同一风格与速度：低沉的古琴拨弦与缓慢弦乐，"
    "节奏平稳、起伏克制、冷冽肃杀，**不要出现明显的段落结束感或终止感**"
)
BOARD_RULE_LINE = (
    "{board} 是分镜参考图片，**只用于参考场景与构图**；人物的动作、朝向与走位以本段文字描述为准，"
    "不要照搬分镜图里的动作"
)
DEFAULT_NEGATIVE = (
    "对白字幕、旁白字幕、台词字幕、片头字、画面标题、贴纸、水印、界面文字、"
    "现代物品、手机、汽车、霓虹、西装、义体、废墟、宫灯、玉玺、圣旨、朝服、"
    "赛博朋克机械结构"
)
#: 有对白 / 无对白两种人声约束（无对白段落必须显式禁止人声，否则模型会自己"说话"，
#: 生成听不懂的语音——这是实测踩到的坑）
VOICE_RULE_WITH_DIALOGUE = (
    "本段**只有上述台词**是人声，不要生成任何其它人声（不要有旁白、吟唱、群杂、背景人声）"
)
VOICE_RULE_NO_DIALOGUE = (
    "本段**没有任何对白**，不要生成任何人声（不要出现说话声、念白声、吟唱声、气声、耳语），"
    "只保留环境音效与物理音"
)

#: 角色设定图（三视图）的固定后缀要求
CHAR_SHEET_RULE = (
    "角色三视图设定图：同一个角色并排展示正面、侧面、背面三个视角，全身完整入镜，"
    "纯白色背景，三个视角的角色造型、服装、发型必须完全一致。"
)


class ScriptError(RuntimeError):
    """剧本内容不合格，错误信息需说明是哪个字段、该怎么改。"""


# --------------------------------------------------------------------------
# 与 DeepSeek 交互
# --------------------------------------------------------------------------


def build_story_messages(
    brief: str,
    *,
    total_seconds: float,
    segment_seconds: float,
    aspect: str,
    character_count: int,
    style_hint: str = "",
) -> list[dict[str, str]]:
    """构造让 DeepSeek 产出剧本内容的对话消息。"""
    count = max(1, round(total_seconds / segment_seconds))
    style_block = ""
    if style_hint.strip():
        style_block = (
            f"\n画面风格（必须贯彻到每一个镜头，并**逐字体现到 sheet_prompt 与 "
            f"storyboard_prompt 的文字描述里**）：{style_hint.strip()}\n"
        )
    system = f"""你是短视频分镜编剧，为 AI 视频模型（MiniMax-H3 多参考图模式）准备素材。

任务：把用户给的主题/讲稿，扩写成一条 {total_seconds:.0f} 秒的连续短片剧本，拆成 {count} 段，
每段 {segment_seconds:.0f} 秒，画幅 {aspect}，主要角色 {character_count} 个。
{style_block}
硬性要求：
1. 剧情必须**连续**：上一段结尾与下一段开头能自然相接（同一场景、同一时间线）。
2. 每段正好 3 个镜头，时间区间必须首尾相接并铺满 0 到 {segment_seconds:.0f} 秒
   （例如 0-5、5-10、10-15）。
3. **每个镜头的 desc 里必须逐镜点名角色**（写"陈默/林雨"），**禁止**用"两人、他俩、他们、
   二人、一对男女、两个人"这类笼统写法；角色一出现在画面里，就同时写明他那件外套的颜色
   与关键配饰（例如"陈默（深灰色连帽卫衣、背黑色双肩包）紧跟在林雨（米白色羽绒外套）
   身后"）。这一条是为了防止视频模型把主角画成随便两个人、或者中途换掉衣服颜色。
   写 `storyboard_prompt` 时只要画到人物，也照此写清楚两人各自的服装颜色。
   `end_state` 同样要点名。
4. 台词与旁白**按故事需要写**（不强制零台词）：由视频模型直接说出来的对白每段最多 1-2 句、每句不超过 20 个汉字（多了它念不完、容易烧成字幕），并写明说话人的声音特征（例如：清澈清冷的女声 / 低沉沙哑的男声），同一角色跨段必须一致；需要更多台词或多段旁白时，写成后期配音用的条目（旁白用 `旁白（音色描述）：文本` 的写法），由 TTS 配音后混进成片。
5. 不要在画面里出现任何文字、字幕、标题、招牌、匾额文字（这是硬性要求，
   因为模型会错误地把对白烧成字幕）。
6. 对白必须写成固定格式：`角色名（神态或动作，声音特征）说：“台词”`，
   例如：`林雨（急促地回头，清亮发颤的女声）说：“往这边跑！”`；
   旁白写成 `旁白（音色描述）：文本`（旁白**不进画面**，只做后期配音）。
7. `scene_anchor` 只写地点与时间点本身（例如"深夜地下车库通道"），
   不要以"同一"开头，也不要带句号。
8. 角色外观要具体到可用于生成设定图：年龄、性别、发型、服装与衣服颜色、配饰、气质。
9. `end_state` 必须写清楚本段**最后一个瞬间人物的朝向、位置与动作**，并点名是谁
   （例如：陈默背对高架、面朝画面右侧的出城方向迈步，林雨在他右后方半步），
   用于避免模型把动作方向做反。
10. `storyboard_prompt` 描述的分镜构图必须与 `shots` 里的动作方向一致
   （人物是走来还是离去、面朝哪边），不要只画风景。
11. 只输出 JSON，不要输出解释文字，不要用 Markdown 代码块包裹。

JSON 结构：
{{
  "title": "作品名（中文，不超过 12 字）",
  "logline": "一句话概要",
  "characters": [
    {{"name": "角色名", "appearance": "外观细节（中文，60 字以内）",
      "sheet_prompt": "用于生成该角色三视图的完整中文提示词"}}
  ],
  "sets": ["主要场景，例如：雪夜宗门山门长阶"],
  "segments": [
    {{"summary": "本段一句话概括",
      "scene_anchor": "本段场景与时间（用于跨段连续性说明）",
      "style": "本段氛围短语（不含时长画幅，例如：肃杀压迫的氛围）",
      "shots": [
        {{"start": 0, "end": 5, "desc": "景别+机位+动作，必要时写对白"}},
        {{"start": 5, "end": 10, "desc": "……"}},
        {{"start": 10, "end": 15, "desc": "……"}}
      ],
      "end_state": "本段最后一个瞬间的朝向/位置/动作",
      "sound": "本段音效清单，用顿号分隔",
      "storyboard_prompt": "该段分镜图的完整中文提示词（交代景别、构图、人物位置、环境、氛围，宽画幅）"}}
  ]
}}

注意：segments 数组必须有 {count} 个元素，顺序即播放顺序。"""

    user = f"""主题/讲稿如下（可能很粗糙，请合理扩写）：

{brief}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# 剧本内容校验与解析
# --------------------------------------------------------------------------


@dataclass
class Shot:
    start: float
    end: float
    desc: str


@dataclass
class Character:
    name: str
    appearance: str
    sheet_prompt: str


@dataclass
class SegmentPlan:
    summary: str
    scene_anchor: str
    style: str
    shots: list[Shot]
    sound: str
    storyboard_prompt: str
    end_state: str = ""


@dataclass
class Story:
    title: str
    logline: str
    characters: list[Character]
    sets: list[str] = field(default_factory=list)
    segments: list[SegmentPlan] = field(default_factory=list)


def _require(data: dict, key: str, where: str) -> Any:
    value = data.get(key)
    if value in (None, "", []):
        raise ScriptError(f"{where} 缺少字段 {key}（空值）\n解决办法：检查 DeepSeek 返回内容是否完整。")
    return value


def parse_story(text: str, *, expected_segments: int, segment_seconds: float) -> Story:
    """解析并校验 DeepSeek 返回的剧本 JSON。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned, flags=re.S)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ScriptError(
            f"DeepSeek 返回的不是合法 JSON：{exc}\n原始内容片段：\n{text[:600]}"
        ) from exc

    characters = [
        Character(
            name=str(_require(c, "name", f"characters[{i}]")),
            appearance=str(_require(c, "appearance", f"characters[{i}]")),
            sheet_prompt=str(_require(c, "sheet_prompt", f"characters[{i}]")),
        )
        for i, c in enumerate(data.get("characters") or [])
    ]
    if not characters:
        raise ScriptError("剧本里没有任何角色（characters 为空）。")

    raw_segments = data.get("segments") or []
    if len(raw_segments) != expected_segments:
        raise ScriptError(
            f"分段数量不对：需要 {expected_segments} 段，实际 {len(raw_segments)} 段。\n"
            f"解决办法：重跑剧本生成，或在 prompts 里手工调整。"
        )

    segments: list[SegmentPlan] = []
    for index, raw in enumerate(raw_segments, start=1):
        where = f"segments[{index - 1}]"
        shots_raw = raw.get("shots") or []
        if not shots_raw:
            raise ScriptError(f"{where} 没有任何镜头（shots 为空）。")
        shots: list[Shot] = []
        for shot_index, shot in enumerate(shots_raw):
            try:
                start = float(shot["start"])
                end = float(shot["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ScriptError(f"{where}.shots[{shot_index}] 的 start/end 不是数字。") from exc
            desc = str(shot.get("desc") or "").strip()
            if not desc:
                raise ScriptError(f"{where}.shots[{shot_index}] 的 desc 为空。")
            shots.append(Shot(start=start, end=end, desc=desc))
        # 时间区间必须首尾相接并铺满整段
        if abs(shots[0].start) > 0.01:
            raise ScriptError(f"{where} 第一个镜头必须从 0 秒开始，实际 {shots[0].start}。")
        for a, b in zip(shots, shots[1:]):
            if abs(a.end - b.start) > 0.01:
                raise ScriptError(f"{where} 镜头时间不连续：{a.end} -> {b.start}（应首尾相接）。")
        if abs(shots[-1].end - segment_seconds) > 0.01:
            raise ScriptError(
                f"{where} 最后一个镜头应结束于 {segment_seconds:.0f} 秒，实际 {shots[-1].end}。"
            )
        segments.append(SegmentPlan(
            summary=str(_require(raw, "summary", where)),
            scene_anchor=str(_require(raw, "scene_anchor", where)),
            style=str(raw.get("style") or ""),
            shots=shots,
            sound=str(raw.get("sound") or "风雪声、衣袍摩擦声"),
            storyboard_prompt=str(_require(raw, "storyboard_prompt", where)),
            end_state=str(raw.get("end_state") or ""),
        ))

    return Story(
        title=str(data.get("title") or "未命名"),
        logline=str(data.get("logline") or ""),
        characters=characters,
        sets=[str(s) for s in (data.get("sets") or [])],
        segments=segments,
    )


# --------------------------------------------------------------------------
# 按模板组装提示词（格式由代码保证）
# --------------------------------------------------------------------------


def char_image_name(index: int) -> str:
    return f"char-{index}.png"


def board_image_name(segment_index: int) -> str:
    return f"board-{segment_index:02d}.png"


def character_lock_line(story: Story) -> str:
    """生成【人物锁定】行：写死本段只出现哪几名角色、各自长什么样、颜色不许改。

    这一行是 2026-10-13 实测加上的补丁：只靠"@图片N角色"声明 + "两人在跑"这类笼统描述，
    模型会把角色当成"随便两个人"，把外套颜色画成另一个颜色（实测 seg-07/seg-08 的女主
    从米白羽绒漂成橄榄绿）。所以这里逐条写出角色外观与参考图编号，并明确禁止替换与换色。
    """
    people = "；".join(
        f"@图片{i + 1}“{c.name}”（{c.appearance.strip().rstrip('。')}）"
        for i, c in enumerate(story.characters)
    )
    lock = (
        f"【人物锁定】本段画面中只出现下列 {len(story.characters)} 名指定人物，"
        f"**不得替换成陌生面孔、不得增加其它重要角色**：{people}。"
        f"他们的脸型、发型与服装**必须与参考图完全一致、颜色不得改变**"
        f"（外套颜色、裤子颜色、背包与配饰都照参考图来）"
    )
    if len(story.characters) > 1:
        lock += "，各人之间的服装与配饰也不要互换"
    lock += "；景别再远，也要让人能靠服装颜色认出画面里是谁。"
    return lock


def assemble_segment_prompt(
    story: Story,
    index: int,
    *,
    segment_seconds: float,
    aspect: str,
    style_prefix: str,
    bgm_line: str = "",
    negative: str = "",
) -> str:
    """把一段剧本内容装进固定的 H3 中文时间轴模板。

    ``bgm_line`` / ``negative`` 留空时退回模块里的默认常量（国风 3D 动漫档），
    这样历史行为逐字不变；其它风格档由 ``config/styles.yaml`` 传进来。
    """
    seg = story.segments[index]
    parts: list[str] = []

    # 第 1 行：参考图声明（角色图按固定顺序 + 本段分镜图）
    refs = [f"@图片{i + 1}角色“{c.name}”" for i, c in enumerate(story.characters)]
    board_name = board_image_name(index + 1)
    refs.append(f"@图片{len(story.characters) + 1}image“{board_name}”")
    parts.append("，".join(refs) + "。")

    # 第 2 行：禁字幕（放在最前面，模型最容易遵守开头约束）
    parts.append(TEXT_BAN_LINE)
    # 第 3 行：分镜图定位（只参考构图，不照搬动作）+ 素材约束
    parts.append(BOARD_RULE_LINE.format(board=board_name) + "。" + ASSET_RULE_LINE)
    # 第 3.5 行：统一背景音乐（各段一致 + 交叉溶解，接缝才不会断）
    parts.append(bgm_line.strip() or BGM_RULE_LINE)
    # 第 4 行：跨段连续性（第 2 段起）
    if index > 0:
        anchor = seg.scene_anchor.strip().lstrip("同一").rstrip("。，,;； ")
        parts.append(
            f"本段紧接上一段：同一{anchor}，人物造型、服装、发型、光线与上一段完全一致，"
            f"画面从上一段的结尾动作继续"
        )
    # 第 4.5 行：人物锁定（谁出场、长什么样、颜色不许改）——防止模型把主角画成路人
    parts.append(character_lock_line(story))
    # 第 5 行：风格与全局设定
    tone = seg.style or "肃杀压迫的氛围"
    parts.append(f"{style_prefix}{segment_seconds:.0f}秒，{aspect}，{tone}")
    # 第 6 行：人声约束（有对白 / 无对白分别写死）
    has_dialogue = any("说：" in shot.desc for shot in seg.shots)
    parts.append(VOICE_RULE_WITH_DIALOGUE if has_dialogue else VOICE_RULE_NO_DIALOGUE)
    # 第 6 行起：时间轴分镜
    for shot in seg.shots:
        parts.append(f"{shot.start:.0f}-{shot.end:.0f}秒：{shot.desc}")
    # 明确本段的收尾状态（防止动作方向做反）
    if seg.end_state:
        parts[-1] = parts[-1] + f"本段最后一个瞬间：{seg.end_state}"
    # 末行：声音与负面
    parts[-1] = parts[-1] + f"【声音】{seg.sound}。负面：{negative.strip() or DEFAULT_NEGATIVE}。"
    return "\n".join(parts)


def write_production(
    project_dir: Path,
    story: Story,
    *,
    segment_seconds: float,
    aspect: str,
    width: int,
    height: int,
    style_prefix: str,
    base_seed: int,
    brief: str = "",
    bgm_line: str = "",
    negative: str = "",
    material_style: str = "",
) -> dict[str, Path]:
    """把剧本落成一个完整的生产目录，返回写出的文件清单。"""
    (project_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (project_dir / "materials").mkdir(parents=True, exist_ok=True)
    (project_dir / "segments").mkdir(parents=True, exist_ok=True)
    (project_dir / "final").mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    char_names = [char_image_name(i + 1) for i in range(len(story.characters))]
    boards = [board_image_name(i + 1) for i in range(len(story.segments))]

    # 1) 各段提示词
    for index in range(len(story.segments)):
        prompt = assemble_segment_prompt(
            story, index, segment_seconds=segment_seconds, aspect=aspect,
            style_prefix=style_prefix, bgm_line=bgm_line, negative=negative,
        )
        path = project_dir / "prompts" / f"seg-{index + 1:02d}.txt"
        path.write_text(prompt, encoding="utf-8")
        written[f"prompt_{index + 1}"] = path

    # 2) 生产定义
    project = {
        "name": project_dir.name,
        "target": "t8bridge_9ref",
        "defaults": {"duration_seconds": segment_seconds},
        "segments": [
            {
                "id": f"seg-{i + 1:02d}",
                "prompt_file": str((project_dir / "prompts" / f"seg-{i + 1:02d}.txt")),
                "seed": base_seed + i,
                "images": char_names + [boards[i]],
            }
            for i in range(len(story.segments))
        ],
    }
    project_file = project_dir / "project.json"
    project_file.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    written["project"] = project_file

    # 3) 素材生成任务（角色三视图 + 每段分镜图）
    style_suffix = f"。{material_style.strip()}" if material_style.strip() else ""
    materials = {
        "name": f"{project_dir.name}-materials",
        "target": "qwen_t2i",
        "defaults": {
            "aspect_ratio": "16:9 (Widescreen)",
            "megapixels": 1.0,
            "negative_prompt": "文字、水印、签名、标签、对话框、多余人物、畸形手指、多余肢体、五官扭曲、低质量、模糊、噪点、背景杂乱",
        },
        "segments": [
            {
                "id": f"char-{i + 1}",
                "prompt": f"{CHAR_SHEET_RULE}角色设定：{c.appearance}。{c.sheet_prompt}{style_suffix}",
                "seed": base_seed + 500 + i,
            }
            for i, c in enumerate(story.characters)
        ] + [
            {
                "id": f"board-{i + 1:02d}",
                "prompt": story.segments[i].storyboard_prompt + style_suffix,
                "seed": base_seed + 600 + i,
            }
            for i in range(len(story.segments))
        ],
    }
    materials_file = project_dir / "materials.json"
    materials_file.write_text(json.dumps(materials, ensure_ascii=False, indent=2), encoding="utf-8")
    written["materials"] = materials_file

    # 4) 剧本（给人看的）
    lines = [
        f"# {story.title}",
        "",
        f"**一句话概要**：{story.logline}",
        f"**规格**：{len(story.segments)} 段 × {segment_seconds:.0f} 秒 = "
        f"{len(story.segments) * segment_seconds:.0f} 秒，{aspect}",
        f"**场景**：{'、'.join(story.sets) or '（未填写）'}",
        "",
        "## 角色",
        "",
    ]
    for i, c in enumerate(story.characters, start=1):
        lines.append(f"- **{c.name}**（参考图 {char_image_name(i)}）：{c.appearance}")
    lines += ["", "## 分段剧情", ""]
    for i, seg in enumerate(story.segments, start=1):
        lines.append(f"### 第 {i} 段（{board_image_name(i)} 作分镜参考）")
        lines.append("")
        lines.append(f"**概要**：{seg.summary}")
        lines.append("")
        for shot in seg.shots:
            lines.append(f"- {shot.start:.0f}-{shot.end:.0f} 秒：{shot.desc}")
        lines.append("")
        lines.append(f"**音效**：{seg.sound}")
        lines.append("")
    if brief:
        lines += ["## 原始主题/讲稿", "", "```text", brief.strip(), "```", ""]
    script_file = project_dir / "script.md"
    script_file.write_text("\n".join(lines), encoding="utf-8")
    written["script"] = script_file
    return written
