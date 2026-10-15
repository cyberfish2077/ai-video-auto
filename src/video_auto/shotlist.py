"""外部镜头清单 → 可生产项目（阶段十：《剪枝者》导入）。

与 ``script_gen.py`` 的分工
--------------------------
``script_gen`` 负责"只有讲稿、内容交给 DeepSeek 写"；本模块负责另一半情形：
**内容已经在别处写好了**（例如用户让 ChatGPT 产出的 72 镜头机器可读清单），
不再让 DeepSeek 重写，只把清单翻译成本流水线的生产定义。

因此提示词格式仍然由代码拼装（禁字幕条款、人物锁定、人声约束都逐字复用
``script_gen`` 的常量），保证与历史成片同源同格式。

产出（全部落在 ``projects/<项目名>/``）
-------------------------------------
* ``prompts/seg-XX.txt`` 每镜头的 H3 提示词（运行时的事实来源）
* ``project.json``        视频生产台账定义（t8bridge_9ref，逐镜头 8 秒）
* ``materials.json``      素材台账：角色三视图 + 场景主参考图 + **无人物镜头**的分镜图（qwen_t2i）
* ``boards.json``         分镜图台账（**有角色**的镜头）：多图参考合成，人物身份来自角色图（qwen_multiref）
* ``derive.json``         派生台账：以已审核角色图为身份来源派生其它年龄（qwen_edit）
* ``narration.json``      配音台账：需要配音的镜头（tts_voice_design）
* ``shotlist.md``         给人看的逐镜头清单（参考图、旁白、文件）

生成顺序（有依赖，别打乱）：``materials`` → ``derive`` → ``sync_inputs`` → ``boards``
→ ``sync_inputs`` → 视频。分镜图要多图参考，必须先有同步进 ComfyUI input 的角色图。

为什么分镜图要分两份台账（2026-10-13 实测对照）：
文生图（qwen_t2i）画不出指定人物，会自己造一张路人脸；实测镜头 09 被画成"镜子里两个老人"、
镜头 14 把年轻林泽画成穿灰卫衣的陌生人。多图参考（qwen_multiref）把角色图与场景图一起喂进去，
同一镜头的人物身份、服装与构图都对得上（代价：单张 65 秒 vs 20 秒）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import script_gen as sg

#: 剧本 txt 里的镜头标题行：``[01] 8秒 | 暴雨高速公路，第一次死亡``
SHOT_HEADER_RE = re.compile(r"^\[(\d{2})\]\s*(\d+(?:\.\d+)?)\s*秒\s*\|\s*(.*)$")
#: 剧本 txt 里的台词/旁白行：``Dialogue / narration (林泽（旁白）): 我叫林泽。``
DIALOGUE_RE = re.compile(r"^Dialogue\s*/\s*narration\s*\((.+?)\)\s*:\s*(.*)$")

#: 不需要配音的说话人标签（那是音效提示，不是台词）
NON_SPEAKING_LABELS = {"音效", "无声", "音乐"}

#: 配音模式：所有对白与旁白都由后期 TTS 完成，视频模型只出环境声
VOICE_RULE_DUBBED = (
    "本段**不要让画面里的人出声**：不要生成任何人声、旁白、吟唱、气声或耳语，"
    "只保留环境音效与物理音；人物可以有说话的口型与表情，声音由后期单独配"
)

#: 后期配音模式下改用的素材约束：**不要提"对白/台词"这些词**。
#: 依据（2026-10-14 实测）：镜头 40 两次生成都在画面下方烧出中文字幕，
#: 而模板里那句"只保留必要的音效与对白台词"等于在提示词里写下"对白台词"四个字，
#: 对"两个人对视"这类镜头是反向引导。改成只说"环境音效与物理音、不要人声"。
ASSET_RULE_LINE_DUBBED = (
    "本视频参考素材只有图片，没有音频和视频，请勿生成音频标签和视频标签；"
    "此视频只需要环境音效与物理音，**画面里没有任何人说话**"
)

#: 多图参考出分镜图时的负面词：除了禁文字，还要挡住"拼图/分格/三视图"——
#: 多图参考工作流很容易把几张参考图并排画出来，那是没用的
BOARD_NEGATIVE = (
    "文字、水印、字幕、标签、签名、对话框、拼图、分格、多格、三视图、设定图、"
    "多余人物、畸形手指、多余肢体、五官扭曲、低质量、模糊、噪点、动漫、插画"
)

#: 多图参考工作流最多 4 个参考图槽位
MAX_REFERENCE_SLOTS = 4


class ShotlistError(RuntimeError):
    """镜头清单或资产表不合格，错误信息需说明是哪个镜头、该怎么改。"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Character:
    key: str
    name_zh: str
    image: str
    appearance: str
    sheet_prompt: str = ""
    derive_from: str = ""
    edit_prompt: str = ""


@dataclass
class Environment:
    key: str
    name_zh: str
    image: str
    prompt: str = ""


@dataclass
class VoiceSpec:
    label: str
    instruct: str
    seed: int


@dataclass
class Shot:
    shot_id: str
    duration: float
    title: str
    action_zh: str
    prompt_en: str
    sound_en: str
    music_en: str
    continuity_en: str
    narration_zh: str = ""
    speaker: str = ""
    refs: list[str] = field(default_factory=list)

    @property
    def seg_id(self) -> str:
        return f"seg-{self.shot_id}"

    @property
    def board_file(self) -> str:
        return f"board-{self.shot_id}.png"

    @property
    def prompt_file(self) -> str:
        return f"prompts/{self.seg_id}.txt"


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value]


def load_assets(path: Path) -> dict[str, Any]:
    """读取资产登记表（角色 / 场景 / 音色）。"""
    if not path.exists():
        raise ShotlistError(
            f"找不到资产登记表：{path}\n解决办法：按 projects/jianzhizhe/assets.yaml 的格式新建一份。"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ShotlistError(f"资产登记表不是合法 YAML：{path}\n{exc}") from exc

    characters: dict[str, Character] = {}
    for key, item in (raw.get("characters") or {}).items():
        item = item or {}
        characters[str(key)] = Character(
            key=str(key),
            name_zh=str(item.get("name_zh") or key),
            image=str(item.get("image") or f"{key.lower()}.png"),
            appearance=str(item.get("appearance") or ""),
            sheet_prompt=str(item.get("sheet_prompt") or ""),
            derive_from=str(item.get("derive_from") or ""),
            edit_prompt=str(item.get("edit_prompt") or ""),
        )
    if not characters:
        raise ShotlistError(f"资产登记表里没有任何角色：{path}")

    environments: dict[str, Environment] = {}
    for key, item in (raw.get("environments") or {}).items():
        item = item or {}
        environments[str(key)] = Environment(
            key=str(key),
            name_zh=str(item.get("name_zh") or key),
            image=str(item.get("image") or f"{key.lower()}.png"),
            prompt=str(item.get("prompt") or ""),
        )

    voices: dict[str, VoiceSpec] = {}
    for label, item in (raw.get("voices") or {}).items():
        item = item or {}
        voices[str(label)] = VoiceSpec(
            label=str(label),
            instruct=str(item.get("instruct") or ""),
            seed=int(item.get("seed") or 7000),
        )

    return {
        "characters": characters,
        "environments": environments,
        "voices": voices,
        "material_style": str(raw.get("material_style") or ""),
        "mirror_rule": str(raw.get("mirror_rule") or ""),
        #: 逐镜头覆盖：{"40": {"action_zh": "换掉的纯画面描述"}}
        "shot_overrides": {
            str(key).zfill(2): dict(value or {})
            for key, value in (raw.get("shot_overrides") or {}).items()
        },
    }


def load_shotlist(path: Path) -> tuple[dict[str, Any], list[Shot]]:
    """读取 72 镜头机器可读清单。"""
    if not path.exists():
        raise ShotlistError(f"找不到镜头清单：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ShotlistError(f"镜头清单不是合法 JSON：{path}\n{exc}") from exc

    shots: list[Shot] = []
    for index, item in enumerate(data.get("shots") or [], start=1):
        shot_id = str(item.get("shot_id") or f"{index:02d}")
        action = str(item.get("visual_action_zh") or "").strip()
        prompt_en = str(item.get("h3_prompt_en") or "").strip()
        if not action and not prompt_en:
            raise ShotlistError(f"镜头 {shot_id} 既没有 visual_action_zh 也没有 h3_prompt_en。")
        shots.append(Shot(
            shot_id=shot_id,
            duration=float(item.get("duration_seconds") or 0) or 8.0,
            title=str(item.get("title") or "").strip(),
            action_zh=action,
            prompt_en=prompt_en,
            sound_en=str(item.get("soundscape_guidance_en") or "").strip(),
            music_en=str(item.get("music_guidance_en") or "").strip(),
            continuity_en=str(item.get("continuity_guidance_en") or "").strip(),
            narration_zh=str(item.get("dialogue_or_narration_zh") or "").strip(),
            refs=_as_list(item.get("reference_asset_keys")),
        ))
    if not shots:
        raise ShotlistError(f"镜头清单里没有任何镜头：{path}")

    meta = {
        "project": str(data.get("project") or path.stem),
        "version": str(data.get("version") or ""),
        "target_total_seconds": data.get("target_total_seconds"),
        "shot_count": data.get("shot_count") or len(shots),
    }
    return meta, shots


def load_script_speakers(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """从人读剧本 txt 里取每个镜头的说话人标签与台词原文。

    机器可读清单里的 ``dialogue_or_narration_zh`` **只有文本没有说话人**，
    而配音需要区分旁白 / 镜中林泽 / 爷爷等音色，所以说话人只能从 txt 里读。
    返回 ``(说话人, 台词)``，键是镜头编号。
    """
    if not path.exists():
        raise ShotlistError(
            f"找不到剧本 txt（取说话人用）：{path}\n"
            f"解决办法：把 MiniMax-H3 分段电影剧本放在同一目录，或用 --script 指定。"
        )
    speakers: dict[str, str] = {}
    lines: dict[str, str] = {}
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        header = SHOT_HEADER_RE.match(line)
        if header:
            current = header.group(1)
            continue
        dialogue = DIALOGUE_RE.match(line)
        if dialogue and current:
            speakers[current] = dialogue.group(1).strip()
            lines[current] = dialogue.group(2).strip()
    return speakers, lines


def attach_script_speakers(
    shots: list[Shot],
    speakers: dict[str, str],
    lines: dict[str, str],
) -> int:
    """把剧本 txt 里的说话人与台词补进镜头对象，返回补上说话人的镜头数。

    机器可读清单里没有说话人字段，配音要区分旁白/镜中林泽等音色，只能走这一步。
    """
    filled = 0
    for shot in shots:
        if shot.shot_id in speakers:
            shot.speaker = speakers[shot.shot_id]
            filled += 1
        if not shot.narration_zh and shot.shot_id in lines:
            shot.narration_zh = lines[shot.shot_id]
    return filled


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def shot_assets(
    shot: Shot,
    characters: dict[str, Character],
    environments: dict[str, Environment],
    *,
    with_board: bool = True,
) -> tuple[list[str], list[str], list[str]]:
    """算出这一镜头要挂哪几张参考图。

    返回 ``(图片文件名列表, 提示词声明片段, 本镜头出现的角色 key)``。
    顺序固定为：角色图 → 场景图 → 分镜图，且与提示词里的 ``@图片N`` 一一对应。
    """
    unknown = [k for k in shot.refs if k not in characters and k not in environments]
    if unknown:
        raise ShotlistError(
            f"镜头 {shot.shot_id} 引用了资产表里没有的 key：{'、'.join(unknown)}\n"
            f"解决办法：在 assets.yaml 里补上这些 key，或改清单里的 reference_asset_keys。"
        )

    images: list[str] = []
    decls: list[str] = []
    used_characters: list[str] = []
    for kind in ("char", "env"):
        for key in shot.refs:
            if kind == "char" and key in characters:
                asset = characters[key]
                used_characters.append(key)
                images.append(asset.image)
                decls.append(f"@图片{len(images)}角色“{asset.name_zh}”")
            elif kind == "env" and key in environments:
                asset = environments[key]
                images.append(asset.image)
                decls.append(f"@图片{len(images)}image“{asset.image}”（{asset.name_zh}场景参考）")
    if with_board:
        images.append(shot.board_file)
        decls.append(f"@图片{len(images)}image“{shot.board_file}”（本镜头分镜图，只参考场景与构图）")
    return images, decls, used_characters


def slot_usage_line(
    characters: dict[str, Character],
    environments: dict[str, Environment],
    images: list[str],
) -> str:
    """生成多图参考的"图几是什么"说明，供合成提示词时引用。"""
    by_image = {asset.image: f"角色“{asset.name_zh}”的身份参考" for asset in characters.values()}
    by_image.update({asset.image: f"场景“{asset.name_zh}”的环境参考" for asset in environments.values()})
    parts: list[str] = []
    for index, name in enumerate(images, start=1):
        parts.append(f"图{index}是{by_image.get(name, name)}")
    return "，".join(parts)


def has_age_variants(characters: dict[str, Character], used: list[str]) -> bool:
    """本镜头是否同时出现"同一个人物的两个年龄版本"。

    判定依据是资产表里的 ``derive_from``（谁是从谁派生出来的），**不写死角色名** ——
    换一部片子、换一批角色 key，这条规则照样成立：同框两人互为派生关系，就要加镜像约束。
    """
    used_set = set(used)
    for key in used:
        asset = characters.get(key)
        if asset and asset.derive_from and asset.derive_from in used_set:
            return True
    return False


def character_lock_line(
    characters: dict[str, Character],
    used: list[str],
    images: list[str],
) -> str:
    """生成【人物锁定】行：本镜头只出现哪几个人、各自长什么样、颜色不许改。"""
    if not used:
        return ""
    people: list[str] = []
    for key in used:
        asset = characters[key]
        slot = images.index(asset.image) + 1
        people.append(f"@图片{slot}“{asset.name_zh}”（{asset.appearance.strip().rstrip('。')}）")
    lock = (
        f"【人物锁定】本镜头画面中只出现下列 {len(used)} 名指定人物，"
        f"**不得替换成陌生面孔、不得增加其它重要角色**：{'；'.join(people)}。"
        f"他们的脸型、五官、发型与服装**必须与参考图完全一致、颜色不得改变**"
    )
    if len(used) > 1:
        lock += "，各人之间的服装与配饰也不要互换"
    lock += "；景别再远，也要让人能靠服装颜色认出画面里是谁。"
    return lock


def assemble_prompt(
    shot: Shot,
    *,
    characters: dict[str, Character],
    environments: dict[str, Environment],
    images: list[str],
    decls: list[str],
    used_characters: list[str],
    style_prefix: str,
    aspect: str,
    negative: str,
    mirror_rule: str = "",
    voice_mode: str = "dubbed",
    previous_title: str = "",
) -> str:
    """把一镜头装进固定的 H3 中文模板（格式与历史成片同源）。"""
    parts: list[str] = []
    parts.append("，".join(decls) + "。")
    parts.append(sg.TEXT_BAN_LINE)
    asset_rule = ASSET_RULE_LINE_DUBBED if voice_mode == "dubbed" else sg.ASSET_RULE_LINE
    parts.append(sg.BOARD_RULE_LINE.format(board=shot.board_file) + "。" + asset_rule)
    if previous_title:
        parts.append(
            f"本段紧接上一段的结尾画面（上一段是“{previous_title}”）：同一人物的造型、服装、"
            f"发型、光线与画面质感必须与上一段完全一致，动作从上一段的结尾继续"
        )
    lock = character_lock_line(characters, used_characters, images)
    if lock:
        parts.append(lock)
    # 同一张脸的年轻版 + 年长版同框（镜面/对视）时，额外写死"这是同一个人"
    if mirror_rule and has_age_variants(characters, used_characters):
        parts.append(f"【镜像约束】{mirror_rule}")
    parts.append(f"{style_prefix}{shot.duration:.0f}秒，{aspect}，{shot.title}")
    parts.append(VOICE_RULE_DUBBED if voice_mode == "dubbed" else sg.VOICE_RULE_NO_DIALOGUE)
    parts.append(f"0-{shot.duration:.0f}秒：{shot.action_zh}　{shot.prompt_en}")
    if shot.continuity_en:
        parts.append(f"【连续性】{shot.continuity_en}")
    if shot.sound_en:
        parts.append(f"【声音】{shot.sound_en}")
    if shot.music_en:
        parts.append(f"【音乐】{shot.music_en}")
    parts.append(f"负面：{negative.strip()}。")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def write_production(
    project_dir: Path,
    *,
    name: str,
    meta: dict[str, Any],
    shots: list[Shot],
    assets: dict[str, Any],
    style_prefix: str,
    aspect: str,
    negative: str,
    material_style: str = "",
    base_seed: int = 20270101,
    chain_mode: str = "same-env",
    voice_mode: str = "dubbed",
) -> dict[str, Path]:
    """写出全套定义文件，返回 ``{用途: 路径}``。"""
    characters: dict[str, Character] = assets["characters"]
    environments: dict[str, Environment] = assets["environments"]
    voices: dict[str, VoiceSpec] = assets["voices"]
    mirror_rule = str(assets.get("mirror_rule") or "")

    (project_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (project_dir / "materials").mkdir(parents=True, exist_ok=True)
    (project_dir / "segments").mkdir(parents=True, exist_ok=True)
    (project_dir / "final").mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    video_segments: list[dict[str, Any]] = []
    materials_items: list[dict[str, Any]] = []
    boards_items: list[dict[str, Any]] = []
    derive_items: list[dict[str, Any]] = []
    narration_items: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    style_suffix = f"。{material_style.strip()}" if material_style.strip() else ""
    previous_envs: set[str] = set()

    # 逐镜头覆盖清单里不合适的文字（例如把旁白句误写进"画面动作"）
    overrides: dict[str, dict[str, Any]] = assets.get("shot_overrides") or {}
    for shot in shots:
        patch = overrides.get(shot.shot_id)
        if not patch:
            continue
        if patch.get("action_zh"):
            shot.action_zh = str(patch["action_zh"])
        if patch.get("prompt_en"):
            shot.prompt_en = str(patch["prompt_en"])

    for index, shot in enumerate(shots):
        images, decls, used = shot_assets(shot, characters, environments)
        prompt = assemble_prompt(
            shot,
            characters=characters,
            environments=environments,
            images=images,
            decls=decls,
            used_characters=used,
            style_prefix=style_prefix,
            aspect=aspect,
            negative=negative,
            mirror_rule=mirror_rule,
            voice_mode=voice_mode,
            previous_title=(shots[index - 1].title if index else ""),
        )
        path = project_dir / shot.prompt_file
        path.write_text(prompt, encoding="utf-8")
        written[f"prompt_{shot.shot_id}"] = path

        envs = {k for k in shot.refs if k in environments}
        chain_prev = ""
        if index and chain_mode != "none":
            same_env = bool(envs & previous_envs)
            if chain_mode == "all" or same_env:
                chain_prev = shots[index - 1].seg_id
        previous_envs = envs

        video_segments.append({
            "id": shot.seg_id,
            "prompt_file": f"projects/{name}/{shot.prompt_file}",
            "seed": base_seed + index,
            "duration_seconds": shot.duration,
            "images": images,
            **({"chain_prev": chain_prev} if chain_prev else {}),
        })

        speaker = shot.speaker
        voice = voices.get(speaker)
        if speaker and speaker not in NON_SPEAKING_LABELS and shot.narration_zh and voice and voice.instruct:
            narration_items.append({
                "id": f"nar-{shot.shot_id}",
                "prompt": shot.narration_zh,
                "seed": voice.seed,
                "extra": {
                    "instruct": voice.instruct,
                    "language": "Chinese",
                    "voice_model": "1.7B",
                },
            })

        rows.append({
            "shot_id": shot.shot_id,
            "title": shot.title,
            "duration": shot.duration,
            "speaker": speaker or "（无台词）",
            "narration": shot.narration_zh,
            "images": images,
            "prompt_file": shot.prompt_file,
            "chain_prev": chain_prev,
            "refs": shot.refs,
        })

    # 1) 视频生产定义
    project_file = project_dir / "project.json"
    project_file.write_text(json.dumps({
        "name": name,
        "target": "t8bridge_9ref",
        "source_shotlist": f"{meta.get('project')} v{meta.get('version')}（{len(shots)} 镜头）",
        "defaults": {"duration_seconds": shots[0].duration, "chain_mode": chain_mode},
        "segments": video_segments,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    written["project"] = project_file

    # 2) 素材台账：基础角色三视图（能自己生成的）+ 场景图 + 分镜图
    for key, asset in characters.items():
        if asset.derive_from:
            continue
        if not any(shot.refs and key in shot.refs for shot in shots):
            continue
        materials_items.append({
            "id": f"{key.lower().replace('_', '-')}",
            "prompt": f"{sg.CHAR_SHEET_RULE}角色设定：{asset.appearance}。{asset.sheet_prompt}{style_suffix}",
            "seed": base_seed + 500,
            "extra": {"aspect_ratio": "16:9 (Widescreen)", "megapixels": 1.0},
        })
    for key, asset in environments.items():
        if not any(key in shot.refs for shot in shots):
            continue
        materials_items.append({
            "id": f"{key.lower().replace('_', '-')}",
            "prompt": asset.prompt + style_suffix,
            "seed": base_seed + 600,
            "extra": {"aspect_ratio": "16:9 (Widescreen)", "megapixels": 1.0},
        })
    for index, shot in enumerate(shots):
        # 分镜图：用中文画面描述 + 英文原始提示，明确"16:9 首帧/分镜"
        board_prompt = (
            f"电影感分镜参考图（16:9 宽画幅，单帧，不是拼图、不是三视图）：{shot.action_zh}"
            f"　{shot.prompt_en}"
            f"　构图交代清楚景别、机位、人物位置与光线方向；画面里不要出现任何文字、字幕、水印。"
        )
        board_item = {
            "id": f"board-{shot.shot_id}",
            "prompt": board_prompt + style_suffix,
            "seed": base_seed + 900 + index,
            "extra": {"aspect_ratio": "16:9 (Widescreen)", "megapixels": 1.0},
        }
        board_images, _decls, used = shot_assets(
            shot, characters, environments, with_board=False)
        if not used:
            # 画面里没有指定人物的镜头：文生图就够，20 秒一张
            materials_items.append(board_item)
            continue
        if len(board_images) > MAX_REFERENCE_SLOTS:
            raise ShotlistError(
                f"镜头 {shot.shot_id} 需要 {len(board_images)} 张参考图，超过多图参考工作流的 "
                f"{MAX_REFERENCE_SLOTS} 个槽位：{'、'.join(board_images)}\n"
                f"解决办法：在 assets.yaml 里合并同类场景参考，或该镜头改用文生图分镜（人工指定）。"
            )
        # 有角色的镜头：多图参考合成，人物身份/服装与场景布局都从参考图来
        board_item["prompt"] = (
            f"{board_prompt}　参考图用法：{slot_usage_line(characters, environments, board_images)}。"
            f"人物必须与身份参考图是**同一张脸、同一套服装**（颜色不许改）；"
            f"场景的空间布局、机位与光线以环境参考图为准。" + style_suffix
        )
        board_item["images"] = board_images
        board_item["extra"] = {"negative_prompt": BOARD_NEGATIVE}
        boards_items.append(board_item)
    materials_file = project_dir / "materials.json"
    materials_file.write_text(json.dumps({
        "name": f"{name}-materials",
        "target": "qwen_t2i",
        "defaults": {
            "negative_prompt": "文字、水印、签名、标签、对话框、多余人物、畸形手指、多余肢体、五官扭曲、低质量、模糊、噪点、背景杂乱、动漫、插画",
        },
        "segments": materials_items,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    written["materials"] = materials_file

    # 2.5) 分镜图台账（有角色的镜头）：多图参考合成，人物身份来自角色图
    boards_file = project_dir / "boards.json"
    if boards_items:
        boards_file.write_text(json.dumps({
            "name": f"{name}-boards",
            "target": "qwen_multiref",
            "defaults": {"negative_prompt": BOARD_NEGATIVE},
            "segments": boards_items,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written["boards"] = boards_file

    # 3) 派生台账：以已审核角色图派生其它年龄（图像编辑）
    for key, asset in characters.items():
        if not asset.derive_from:
            continue
        parent = characters.get(asset.derive_from)
        if parent is None:
            raise ShotlistError(f"{key} 的 derive_from 指向不存在的角色：{asset.derive_from}")
        derive_items.append({
            "id": f"{key.lower().replace('_', '-')}",
            "prompt": asset.edit_prompt + style_suffix,
            "seed": base_seed + 700,
            "images": [parent.image],
            "extra": {"negative_prompt": "文字、水印、签名、标签、多余人物、畸形手指、五官扭曲、低质量、模糊"},
        })
    derive_file = project_dir / "derive.json"
    if derive_items:
        derive_file.write_text(json.dumps({
            "name": f"{name}-derive",
            "target": "qwen_edit",
            "defaults": {},
            "segments": derive_items,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written["derive"] = derive_file

    # 4) 配音台账
    narration_file = project_dir / "narration.json"
    if narration_items:
        narration_file.write_text(json.dumps({
            "name": f"{name}-narration",
            "target": "tts_voice_design",
            "defaults": {"language": "Chinese", "voice_model": "1.7B"},
            "segments": narration_items,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written["narration"] = narration_file

    # 5) 给人看的清单
    lines = [
        f"# {name} 镜头生产清单（自动生成，勿手改）",
        "",
        f"- 来源清单：{meta.get('project')} v{meta.get('version')}"
        f"（原清单 {meta.get('shot_count')} 镜头 / {meta.get('target_total_seconds')} 秒）",
        f"- 本次导入：{len(shots)} 镜头，合计 {sum(s.duration for s in shots):.0f} 秒",
        f"- 连续性锚点模式：{chain_mode}",
        f"- 人声模式：{voice_mode}（对白与旁白交给后期 TTS）",
        "",
        "| 镜头 | 时长 | 标题 | 说话人 | 参考图（@图片顺序） | 锚点 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        images_txt = " → ".join(str(x) for x in row["images"])
        lines.append(
            f"| {row['shot_id']} | {row['duration']:.0f}s | {row['title']} | {row['speaker']} | "
            f"{images_txt} | {row['chain_prev'] or '—'} |"
        )
    lines += ["", "## 逐镜头台词（配音台账用）", ""]
    for row in rows:
        if row["narration"]:
            lines.append(f"- **{row['shot_id']}**（{row['speaker']}）：{row['narration']}")
    shotlist_file = project_dir / "shotlist.md"
    shotlist_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["shotlist"] = shotlist_file
    return written
