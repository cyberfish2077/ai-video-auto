"""阶段 3：大纲与正式剧本。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe director_system\\scripts\\make_story.py ^
        --film-id animal-heads-nap-01 --concept c2

前置：关卡 A 必须是 APPROVED（否则本程序直接拒绝运行——这是真实停机点，不是提示词里的"请等待确认"）。

本程序会：

1. 读创作简报、已批准的概念、以及**独立评审给这个概念的"最小修法"**；
2. 调 DeepSeek 生成故事大纲（prompts/02_story_outline.md），用 story_outline.schema.json 校验；
3. 调 DeepSeek 写出正式剧本（prompts/03_screenplay.md），用 screenplay.schema.json 校验；
4. 调 DeepSeek 做一次独立评审（prompts/06_quality_review.md），用 artifact_review.schema.json 校验；
5. 写出 story_outline.json/.md、screenplay.json/.md、screenplay_review.json/.md；
6. 把关卡 B 置为 PENDING_HUMAN_REVIEW。

不会做：写 H3 提示词、出分镜图、提交 ComfyUI、修改工作流。
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_auto import config as cfg  # noqa: E402
from video_auto import deepseek as ds  # noqa: E402

import make_ideas  # noqa: E402  # 复用：call_json / schema_errors / load_schema / 归档


# ----------------------------------------------------------------------
# 关卡与输入
# ----------------------------------------------------------------------


def load_state(film_dir: Path, conf: dict) -> dict:
    state_path = film_dir / str(conf["film_layout"]["state_file"])
    if not state_path.is_file():
        raise FileNotFoundError(
            f"找不到状态文件：{state_path}\n解决办法：先跑阶段 2（make_ideas.py）建立状态。"
        )
    return json.loads(state_path.read_text(encoding="utf-8"))


def require_gate_approved(state: dict, gate_id: str, conf: dict) -> None:
    """真实停机点：上游关卡没批准就直接拒绝运行。"""
    gate = (state.get("gates") or {}).get(gate_id) or {}
    status = str(gate.get("status") or "NOT_STARTED")
    if status == "APPROVED":
        return
    title = next((g["title"] for g in conf["review_gates"] if g["id"] == gate_id), gate_id)
    raise SystemExit(
        f"[停止] 关卡 {gate_id}（{title}）当前是 {status}，不是 APPROVED。\n"
        f"       下一步：请人先审阅并通过关卡 {gate_id}，再重跑本命令：\n"
        f"       .venv\\Scripts\\python.exe director_system\\scripts\\review_gate.py "
        f"--film-id {state.get('film_id')} --approve {gate_id} --note \"你的意见\""
    )


def find_concept(ideas: dict, concept_id: str) -> dict:
    for concept in ideas["concepts"]:
        if concept["id"] == concept_id:
            return concept
    raise SystemExit(
        f"[错误] 提案里没有概念 {concept_id}；可用："
        f"{'、'.join(c['id'] for c in ideas['concepts'])}"
    )


def review_fixes(review: dict | None, concept_id: str, conf: dict) -> list[str]:
    """从独立评审里挑出适用于本概念的"最小修法"（含整体性问题）。

    会丢掉与**当前时长策略**冲突的建议：用户 2026-10-14 明确"优先采用合法时长、不做裁切"，
    所以评审里"裁掉若干帧去对齐整秒"这类修法已失效（否则模型会照做，把成片时长又变成人为凑数的）。
    """
    if not review:
        return []
    trim_allowed = bool((conf.get("duration_policy") or {}).get("trim_in_post", False))
    fixes: list[str] = []
    for issue in review.get("issues", []):
        if str(issue.get("scope")) not in (concept_id, "整体"):
            continue
        text = str(issue.get("smallest_fix") or "")
        if not trim_allowed and any(word in text for word in ("裁掉", "裁切", "裁短")):
            continue
        marker = "致命" if issue.get("severity") == "致命" else str(issue.get("severity"))
        fixes.append(f"[{marker}·{issue.get('category')}] {text}")
    return fixes


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------


def nominal_seconds_of(brief: dict, conf: dict) -> float:
    """简报里的目标时长（创意意图），不是合法帧数。"""
    policy = conf.get("duration_policy") or {}
    return float((brief.get("from_user") or {}).get("duration_seconds")
                 or policy.get("nominal_seconds") or 30)


def story_constraints(conf: dict, brief: dict) -> str:
    policy = conf.get("duration_policy") or {}
    nominal = nominal_seconds_of(brief, conf)
    tolerance = float(policy.get("tolerance_seconds") or 1.5)
    return f"""\
【时长：先按叙事需要，最后才映射到技术限制】
本片规模约 {nominal:g} 秒（±{tolerance:g} 秒）。**在这一阶段（大纲／剧本）按叙事任务分配每场的
理想时长，可以是任意秒数**（例如 3.5 秒、18 秒），不必考虑工作流的帧数限制——
镜头边界与时长由分镜阶段按戏剧节拍决定，映射到合法帧数是更后面的事。
项目规则（director_system/config/director.yaml 的 pacing_policy）：
① 先根据剧本、戏剧节拍、场景变化和镜头运动决定镜头边界；② 再按每个镜头的叙事任务分配时长；
③ 最后才映射到工作流允许的合法帧数；④ 技术限制与叙事冲突时，**优先调整节奏、镜头边界或生成策略，
而不是平均分段**；⑤ 只有故事本身确实需要均匀节奏时才用等长分段。
所以：`duration_intent` 要写清"哪场为什么长／哪场为什么短"，不要为了凑总时长把场次切成均等的几块。
{make_ideas.inherited_opening_constraints(brief)}

【实测制作约束（工作流决定，故事必须能这样拍出来）】
1. 单次生成只能是一段**连续镜头**：一段之内不许换空间（不许"从工位走到茶水间再进洗手间"），
   跨空间的动作要放到下一段，或用"事件走进主角所在空间"的方式解决。
2. 每段画面里清晰可辨的动物头最多 2 个，其余只能是后脑勺、虚焦或剪影。
3. 画面里**绝对不能出现任何文字**：不写工牌、公告栏、文件、屏幕内容、时间数字、门牌；
   道具必须靠形状和颜色成立（例如用红色别针而不是印章 / 工牌照片）。
4. 台词与旁白**按剧本与简报的 dialogue_policy 来**（不强制零台词）：剧本里可以写台词与旁白，但每条都要写清「谁在说／是不是旁白、用什么声音、说什么」（后期要拿这些去配音）；**画面提示词里一律不许出现台词文字**——H3 会把台词烧成画面字幕，且负面提示词无效。
5. 回避正面镜面与屏幕反光（人头与动物头同框反光是崩溃高发场景）。
6. 复杂长动作、多人快速走位、精细手部特写容易崩，宁可用反应镜头与声音暗示。
"""


def outline_user_prompt(brief: dict, concept: dict, fixes: list[str], conf: dict, schema: dict) -> str:
    fix_text = "\n".join(f"- {item}" for item in fixes) or "- （评审没有给出针对本概念的修法）"
    return f"""\
请把下面这个**已经人工批准**的故事概念发展成因果大纲。不要写分镜、不要写 H3 提示词。

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【已批准的概念（{concept['id']} · {concept['title']}）】
{json.dumps(concept, ensure_ascii=False, indent=2)}

{story_constraints(conf, brief)}

【独立评审已经指出、你必须在本大纲里落实的修法】
{fix_text}

【大纲要求】
* 每个主要节拍都要回答：主角此刻要什么、什么挡着、他做了什么选择、后果是什么、
  观众新增什么问题、为什么下一个节拍由它引出；
* 每场戏必须标出**戏剧引擎**（主引擎，可选副引擎），取值只能来自：
  情绪对峙、信息差、权力博弈、抉择、悬念、关系界定、仪式/规则、喜剧、温情；
* 结尾必须由前文铺垫兑现，不许凭空加信息；
* 铺垫/回收要写成对照表（setup ↔ payoff）；
* 老实写风险：逻辑风险、套路风险、制作风险（每条制作风险要给"不改故事的另一种拍法"）。

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文。`total_seconds` 必须等于 {nominal_seconds_of(brief, conf)}。
"""


def screenplay_user_prompt(
    brief: dict, concept: dict, outline: dict, fixes: list[str], conf: dict, schema: dict
) -> str:
    fix_text = "\n".join(f"- {item}" for item in fixes) or "- （没有额外修法）"
    return f"""\
请把下面这份大纲写成**可拍的正式剧本**。不要写分镜表、不要写 H3 提示词。

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【已批准的概念】
{json.dumps(concept, ensure_ascii=False, indent=2)}

【已批准的大纲】
{json.dumps(outline, ensure_ascii=False, indent=2)}

{story_constraints(conf, brief)}

【必须在剧本里落实的修法】
{fix_text}

【剧本要求】
* 用可见动作写，少写心理；台词只承担角色、冲突、潜台词或必要信息，能不说话就不说话；
* 每场戏写清戏剧职能、主角当场目标、新增信息、阻碍、决定、结束时状态的改变；
* 节奏要有变化，不要每场一样长；
* 给一份**审核清单**：人在批准这份剧本之前必须逐条确认的事情（含本工作流特有风险）；
* 高风险场次要单列，并给出规避写法（不改故事）；
* 连续性圣经要写全：角色外观/服装/道具、地理、以及这个世界观里必须一致的规则。

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文。`total_seconds` 必须是 {nominal_seconds_of(brief, conf)}；
`screenplay_text` 是人类可读的完整剧本正文（场次标题 + 动作 + 台词 + 声音）。
"""


def review_user_prompt(
    screenplay: dict, conf: dict, concept: dict, schema: dict, previous: list[str] | None = None,
    brief: dict | None = None,
) -> str:
    check_block = ""
    if previous:
        check_block = f"""\

【上一版评审提出的必修项（请**逐条核对是否真的改到位**；没改到位的必须写进 issues，
并把它再次列入 must_fix_before_next_stage）】
{fixes_numbering(previous)}
"""
    return f"""\
请以独立评审的身份审这份**剧本**（不是你自己写的，不要替它辩护）。
每条问题都要指出：哪一场、哪一类、多严重、证据、最小修法。

【待评审的剧本】
{json.dumps(screenplay, ensure_ascii=False, indent=2)}
{check_block}
{story_constraints(conf, brief or {})}

重点检查：因果链是否闭合；主角有没有能动性与可见代价；戏剧引擎是否每场都在变；
关键信息是否靠画面而不是台词/文字；结尾是否由铺垫兑现；
**台词是否会被烧成字幕**（本工作流把台词画成字幕是实测高发问题，负面词无效）；
每段的时长与"一段连续镜头"约束是否真能落地；连续性（服装/道具/空间/朝向）是否可追踪。

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文，`target` 填 "screenplay"。`status` 必须是 PENDING_HUMAN_REVIEW，
`auto_approve` 必须是 false。`must_fix_before_next_stage` 按优先级排列，可以为空数组。
"""


def must_fix_texts(review: dict) -> list[str]:
    return [str(item) for item in (review.get("must_fix_before_next_stage") or [])]


def issues_text(review: dict) -> str:
    order = {"致命": 0, "严重": 1, "轻微": 2}
    lines: list[str] = []
    for issue in sorted(review.get("issues") or [], key=lambda i: order.get(i.get("severity"), 9)):
        lines.append(
            f"- [{issue.get('severity')}·{issue.get('category')}]（{issue.get('scope')}）"
            f"证据：{issue.get('evidence')}\n  最小修法：{issue.get('smallest_fix')}"
        )
    return "\n".join(lines)


def fixes_numbering(must_fixes: list[str]) -> str:
    return "\n".join(f"必修{i}：{text}" for i, text in enumerate(must_fixes, 1))


PROPAGATION_RULE = """\
【改动必须同步传播（本轮重点，上一轮的返修就栽在这里）】
上一轮返修改对了正文，却没有把"引用同一件事的别处"一起改掉，于是同一枚别针在
rules 里是"事后的标记"、在 SC03 里是"导致消失的原因"，被评审判为致命矛盾。

所以本次返修，凡是改动任何一个事实（别针的语义/枚数/出现位置、谁在场、谁做了动作、
声音形态、空间关系、颜色），都必须在**同一份 JSON 里**检查并同步改掉所有提到它的地方：

* `continuity_bible.rules`（世界观规则）
* `continuity_bible.props`（道具口径：枚数、去向）
* `continuity_bible.geography`（空间、哪把椅子是谁的）
* `continuity_bible.characters`（角色条目：谁在场、什么关系）
* `high_risk_scenes`（风险描述与规避写法）
* 每一场的 `action` / `sound` / `dialogue_policy_note`
* `logline`、`synopsis`（开场与结尾的说法必须与正文一致）
* `review_checklist`（清单里的说法也要跟着改）
* 大纲里的对应字段（`what_happens`、`beats`、`setup_payoff`、`dramatic_change`）

其中**必修1 是致命项**：请原样按它给出的方向改（别针统一为"先别针、后带走"，
别针＝提交／宣布），不要自己另立一套规则。
"""


def outline_revision_prompt(
    brief: dict, concept: dict, conf: dict, outline: dict,
    must_fixes: list[str], issues: str, revision: int, schema: dict,
) -> str:
    return f"""\
这是对**已有大纲**的修订，不是重写。请按必修项逐条改，**其余部分尽量逐字保留**。

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【已批准的概念】
{json.dumps(concept, ensure_ascii=False, indent=2)}

{story_constraints(conf, brief)}

【当前大纲（第 {revision - 1} 版，基于它修订）】
{json.dumps(outline, ensure_ascii=False, indent=2)}

【评审给的必修项（逐条落实，一条都不能漏）】
{fixes_numbering(must_fixes)}

【评审的完整问题清单（背景信息，属于必修项的必须改）】
{issues}

{PROPAGATION_RULE}

【输出要求】
严格按下面的 JSON Schema 输出完整的大纲（不是补丁，是修订后的全文）：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文。`total_seconds` 仍必须是 {nominal_seconds_of(brief, conf)}（合法时长之和，不裁切）。
**`revision` 字段必须写**：`revision` = {revision}；
`applied_fixes` 对**每一条必修项各写一条**，格式必须是以"必修N："开头的字符串
（N 是必修项编号，共 {len(must_fixes)} 条），写清这一条你具体改成了什么；
**另外再加一条以"一致性核对："开头的条目**，写清你为了保持一致而同步改了哪些互相引用的地方
（对照上面那张清单，逐个说明"改了/确认无需改"）。
`unchanged_note` 写你没有改动、保持原样的部分。
"""


def screenplay_revision_prompt(
    brief: dict, concept: dict, conf: dict, outline: dict, screenplay: dict,
    must_fixes: list[str], issues: str, revision: int, schema: dict,
) -> str:
    return f"""\
这是对**已有剧本**的修订，不是重写。请按必修项逐条改，**其余部分尽量逐字保留**
（评审认可的地方——无字道具、每段各锁一个空间、画面里不出现文字——不要动）。

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【已批准的概念】
{json.dumps(concept, ensure_ascii=False, indent=2)}

{story_constraints(conf, brief)}

【修订后的大纲（与剧本必须一致）】
{json.dumps(outline, ensure_ascii=False, indent=2)}

【当前剧本（第 {revision - 1} 版，基于它修订）】
{json.dumps(screenplay, ensure_ascii=False, indent=2)}

【评审给的必修项（逐条落实，一条都不能漏）】
{fixes_numbering(must_fixes)}

【评审的完整问题清单（背景信息，属于必修项的必须改）】
{issues}

{PROPAGATION_RULE}

【输出要求】
严格按下面的 JSON Schema 输出完整的修订版剧本：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
全部用简体中文。`total_seconds` 仍必须是 {nominal_seconds_of(brief, conf)}。
改完之后请自查两件事：①每一场的 `action` 条数在该场时长内**真的演得完**
（安静缓慢的调性下，10.125 秒大约只够 6~8 拍，且要留遮挡换层与抬人的时间）；
②同一个镜头里的取景要求彼此不矛盾。
**`revision` 字段必须写**：`revision` = {revision}；
`applied_fixes` 对**每一条必修项各写一条**，格式必须是以"必修N："开头的字符串
（共 {len(must_fixes)} 条）；**另外再加一条以"一致性核对："开头的条目**，
写清你同步改了哪些互相引用的地方（逐项说明"改了/确认无需改"）；
`unchanged_note` 写保持原样的部分。
"""


def coverage_problems(artifact: dict, expected_count: int, *, require_consistency_note: bool = False) -> list[str]:
    """核验"必修项逐条落实"：applied_fixes 必须覆盖 1..N 每一条。

    ``require_consistency_note`` 为真时（返修模式），还要求额外一条"一致性核对："，
    逼模型去检查所有引用同一事实的字段，而不是只改被点名的地方。
    """
    import re as _re

    applied = (artifact.get("revision") or {}).get("applied_fixes") or []
    problems: list[str] = []
    if len(applied) < expected_count:
        problems.append(
            f"applied_fixes 只有 {len(applied)} 条，必须对 {expected_count} 条必修项各写一条"
        )
    found = set()
    for item in applied:
        match = _re.match(r"必修\s*(\d+)", str(item))
        if match:
            found.add(int(match.group(1)))
    missing = sorted(set(range(1, expected_count + 1)) - found)
    if missing:
        problems.append(
            "applied_fixes 里找不到这些必修项的编号：" + "、".join(f"必修{n}" for n in missing)
            + "（每条都要以「必修N：」开头）"
        )
    if require_consistency_note and not any(
        str(item).startswith("一致性核对") for item in applied
    ):
        problems.append(
            "还缺一条以「一致性核对：」开头的 applied_fixes，用来写清同步改了哪些互相引用的地方"
            "（rules / props / geography / high_risk_scenes / sound / logline / synopsis / review_checklist）"
        )
    return problems


def record_gate(
    state: dict, conf: dict, gate_id: str, *,
    note: str, artifacts: list[str], revision: int | None = None,
) -> None:
    """把某个关卡置为"等人工审阅"（不落盘，由调用方写文件）。

    铁律：**已经 APPROVED 的关卡不会被打回**——重新生成产物不该让人先前的批准作废。
    """
    gates = state.setdefault("gates", {})
    entry = gates.setdefault(gate_id, {})
    gate_cfg = next((g for g in conf["review_gates"] if g["id"] == gate_id), {})
    entry.setdefault("title", gate_cfg.get("title", gate_id))
    if str(entry.get("status") or "") != "APPROVED":
        entry["status"] = conf["initial_gate_state"]
    entry["unlocks"] = entry.get("unlocks") or gate_cfg.get("unlocks", "")
    entry["artifacts"] = artifacts
    if revision:
        entry["revision"] = revision
    entry["note"] = note
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_gate_b(state: dict, conf: dict, review: dict, revision: int | None = None) -> None:
    """剧本关口（关卡 B）的落盘：写清这一版还剩几条必修项。"""
    remaining = len(must_fix_texts(review))
    verdict = make_ideas.convergence_verdict(review, conf, round_no=revision)
    if revision:
        note = (f"第 {revision} 版：已按上一版评审的必修项返修，"
                f"重新评审后剩余必修项 {remaining} 条。{verdict} 等人工审阅。")
    else:
        note = f"第 1 版：独立评审列出必修项 {remaining} 条。{verdict} 等人工审阅。"
    record_gate(
        state, conf, "B",
        note=note,
        artifacts=["story_outline.json", "story_outline.md", "screenplay.json",
                   "screenplay.md", "screenplay_review.json", "screenplay_review.md"],
        revision=revision,
    )


# ----------------------------------------------------------------------
# 人读的 Markdown
# ----------------------------------------------------------------------


def render_outline_md(outline: dict, meta: dict) -> str:
    out = [f"# 故事大纲：{outline['film_id']}（{outline['title']}）", ""]
    out.append(f"- 生成时间：{meta['created_at']}　模型：{meta['model']}")
    if outline.get("revision"):
        out.append(f"- 修订：第 {outline['revision']['revision']} 版（按评审必修项返修，"
                   f"落实 {len(outline['revision']['applied_fixes'])} 条）")
    out.append(f"- 成片时长：{outline['total_seconds']} 秒　（{outline['duration_note']}）")
    out.append(f"- 基于概念：{outline['concept_id']}")
    out.append("")
    out.append("## 故事梗概")
    out.append("")
    out.append(outline["synopsis"])
    out.append("")
    p = outline["protagonist"]
    out.append("## 主角")
    out.append("")
    out.append(f"- 是谁：{p['who']}")
    out.append(f"- 要什么：{p['goal']}")
    out.append(f"- 内心冲突：{p['internal_conflict']}")
    out.append(f"- 赌注：{p['stakes']}")
    out.append("")
    out.append("## 场景表")
    out.append("")
    out.append("| 场次 | 标题 | 地点 | 时长(秒) | 主引擎 | 副引擎 | 主角的选择 | 局面变化 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for scene in outline["scenes"]:
        out.append(f"| {scene['scene_id']} | {scene['title']} | {scene['setting']} | "
                   f"{scene['estimated_seconds']} | {scene['dramatic_engine']} | "
                   f"{scene.get('secondary_engine', '—')} | {scene['protagonist_choice']} | "
                   f"{scene['dramatic_change']} |")
    out.append("")
    out.append("## 节拍表")
    out.append("")
    for beat in outline["beats"]:
        out.append(f"### {beat['beat_id']} · {beat['name']}（{beat['function']}）")
        out.append("")
        out.append(f"- 主角要什么：{beat['protagonist_want']}")
        out.append(f"- 阻碍：{beat['obstacle']}")
        out.append(f"- 他的选择/行动：{beat['choice_action']}")
        out.append(f"- 后果：{beat['consequence']}")
        out.append(f"- 观众新增的问题：{beat['new_question']}")
        out.append(f"- 为什么下一个节拍由它引出：{beat['why_next']}")
        out.append("")
    out.append("## 伏笔 / 回收")
    out.append("")
    out.append("| 铺垫 | 回收 | 说明 |")
    out.append("| --- | --- | --- |")
    for item in outline["setup_payoff"]:
        out.append(f"| {item['setup']} | {item['payoff']} | {item['note']} |")
    out.append("")
    for title, key in (("逻辑风险", "logic_risks"), ("套路风险", "cliche_risks")):
        out.append(f"## {title}")
        out.append("")
        for item in outline[key] or ["（无）"]:
            out.append(f"- {item}")
        out.append("")
    out.append("## 制作风险与替代拍法")
    out.append("")
    for item in outline["production_risks"]:
        out.append(f"- 风险：{item['risk']}")
        out.append(f"  - 不改故事的另一种拍法：{item['staging_alternative']}")
    out.append("")
    return "\n".join(out)


def render_screenplay_md(screenplay: dict, meta: dict) -> str:
    out = [f"# 正式剧本：{screenplay['film_id']}（{screenplay['title']}）", ""]
    out.append(f"- 生成时间：{meta['created_at']}　模型：{meta['model']}")
    if screenplay.get("revision"):
        out.append(f"- 修订：第 {screenplay['revision']['revision']} 版（按评审必修项返修，"
                   f"落实 {len(screenplay['revision']['applied_fixes'])} 条）")
    out.append(f"- 成片时长：{screenplay['total_seconds']} 秒　基于概念：{screenplay['concept_id']}")
    out.append(f"- 台词说明：{screenplay['dialogue_policy_note']}")
    out.append("")
    out.append("## 剧本正文")
    out.append("")
    out.append(screenplay["screenplay_text"])
    out.append("")
    out.append("## 场次拆解")
    out.append("")
    for scene in screenplay["scenes"]:
        out.append(f"### {scene['scene_id']}　{scene['heading']}")
        out.append("")
        out.append(f"- 主引擎：{scene['dramatic_engine']}"
                   + (f"（副：{scene['secondary_engine']}）" if scene.get("secondary_engine") else ""))
        out.append(f"- 戏剧职能：{scene['dramatic_purpose']}　预计 {scene['estimated_seconds']} 秒")
        out.append(f"- 当场目标：{scene['immediate_objective']}")
        out.append(f"- 新增信息：{scene['new_information']}")
        out.append(f"- 阻碍：{scene['obstacle']}")
        out.append(f"- 决定：{scene['decision']}")
        out.append(f"- 结束时状态：{scene['end_state_change']}")
        out.append("")
        out.append("可拍动作：")
        out.append("")
        for line in scene["action"]:
            out.append(f"1. {line}")
        out.append("")
        if scene["dialogue"]:
            out.append("台词：")
            out.append("")
            for line in scene["dialogue"]:
                note = f"（{line['delivery_note']}）" if line.get("delivery_note") else ""
                out.append(f"- {line['speaker']}{note}：“{line['line']}”")
            out.append("")
        else:
            out.append("台词：无")
            out.append("")
        out.append(f"声音：{scene['sound']}")
        out.append("")
    out.append("## 视觉事件清单")
    out.append("")
    for item in screenplay["visual_event_inventory"]:
        out.append(f"- （{item['scene_id']}）{item['event']}　—　{item['why_memorable']}")
    out.append("")
    bible = screenplay["continuity_bible"]
    out.append("## 连续性圣经")
    out.append("")
    for char in bible["characters"]:
        out.append(f"- **{char['name']}**（{char['id']}／{char['role']}）：{char['appearance']}；"
                   f"服装：{char['wardrobe']}")
    for key, label in (("geography", "空间"), ("props", "道具"), ("rules", "世界观规则")):
        if bible[key]:
            out.append(f"- {label}：{'；'.join(bible[key])}")
    out.append("")
    out.append("## 伏笔 / 回收")
    out.append("")
    for item in screenplay["setup_payoff"]:
        out.append(f"- 铺垫：{item['setup']}　→　回收：{item['payoff']}（{ '、'.join(item['scenes']) }）")
    out.append("")
    out.append("## 高风险场次")
    out.append("")
    for item in screenplay["high_risk_scenes"]:
        out.append(f"- {item['scene_id']}：{item['risk']}　→　规避：{item['mitigation']}")
    out.append("")
    out.append("## 审核清单（批这个剧本之前逐条确认）")
    out.append("")
    for item in screenplay["review_checklist"]:
        out.append(f"- [ ] {item}")
    out.append("")
    if screenplay.get("revision"):
        out.append("## 本次修订落实的必修项")
        out.append("")
        for item in screenplay["revision"]["applied_fixes"]:
            out.append(f"- {item}")
        out.append("")
        if screenplay["revision"].get("unchanged_note"):
            out.append(f"未改动的部分：{screenplay['revision']['unchanged_note']}")
            out.append("")
    return "\n".join(out)


def render_review_md(review: dict, meta: dict) -> str:
    order = {"致命": 0, "严重": 1, "轻微": 2}
    out = [f"# 剧本独立评审：{review['film_id']}", ""]
    out.append(f"- 生成时间：{meta['created_at']}　模型：{meta['model']}")
    out.append(f"- 对象：{review['target']}　状态：{review['status']}（不自动放行）")
    out.append(f"- 评审意见：可以进入下一阶段 = {'是（带着必修清单）' if review['recommendation']['next_stage_ready'] else '否'}")
    out.append("")
    out.append("## 结论摘要")
    out.append("")
    out.append(review["summary"])
    out.append("")
    out.append("## 必修项（不修不能进下一阶段）")
    out.append("")
    if review["must_fix_before_next_stage"]:
        for item in review["must_fix_before_next_stage"]:
            out.append(f"- {item}")
    else:
        out.append("- （无）")
    out.append("")
    out.append("## 问题清单")
    out.append("")
    for issue in sorted(review["issues"], key=lambda i: order.get(i["severity"], 9)):
        out.append(f"### [{issue['severity']}] {issue['category']}（{issue['scope']}）")
        out.append("")
        out.append(f"- 证据：{issue['evidence']}")
        out.append(f"- 最小修法：{issue['smallest_fix']}")
        out.append("")
    out.append("## 专项评估")
    out.append("")
    out.append("| 维度 | 判断 | 依据 |")
    out.append("| --- | --- | --- |")
    for item in review["criterion_assessments"]:
        out.append(f"| {item['criterion']} | {item['verdict']} | {item['evidence']} |")
    out.append("")
    if review["strengths"]:
        out.append("## 站得住的地方")
        out.append("")
        for item in review["strengths"]:
            out.append(f"- {item}")
        out.append("")
    rec = review["recommendation"]
    out.append("## 必须由人决定的事")
    out.append("")
    out.append(f"{rec['why']}")
    out.append("")
    for item in rec["what_human_must_decide"]:
        out.append(f"- {item}")
    out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


def revision_checks(artifact: dict, nominal: float, conf: dict, must_fix_count: int) -> list[str]:
    """返修产物的机械校验：时长自洽 + 必修项逐条覆盖 + 一致性核对说明。

    收成显式函数（2026-10-15）：原来写成 lambda 内联在两处调用里，引用了本作用域的
    `nominal`／`must_fixes`，其中一处没定义就直接 NameError（`--revise` 一跑就崩）。
    现在参数全部显式传入，并且 `--dry-run` 会真的调用它，这类错误一跑冒烟就暴露。
    """
    return (duration_problems(artifact, nominal, conf)
            + coverage_problems(artifact, must_fix_count, require_consistency_note=True))


def duration_problems(artifact: dict, nominal: float, conf: dict, *, key: str = "scenes") -> list[str]:
    """创意阶段的时长校验：只看"叙事意图自洽"与"规模落在容差内"，不再要求等于某个合法帧数和。"""
    policy = conf.get("duration_policy") or {}
    tolerance = float(policy.get("tolerance_seconds") or 1.5)
    total = float(artifact.get("total_seconds") or 0)
    problems: list[str] = []
    if abs(total - nominal) > tolerance:
        problems.append(
            f"total_seconds={total} 偏离简报的规模 {nominal:g} 秒超过 {tolerance:g} 秒"
            "（这一阶段按叙事分配即可，但不能离规模太远）"
        )
    scene_sum = sum(float(s.get("estimated_seconds") or 0) for s in artifact.get(key, []))
    # 开篇沿用既有素材的秒数（本片不拍）不计入场次，但要计入全片总时长
    inherited = float(artifact.get("inherited_seconds") or 0)
    if abs(scene_sum + inherited - total) > 0.5:
        extra = f"（另有开篇沿用 {inherited:g} 秒）" if inherited else ""
        problems.append(
            f"各场理想时长之和 {round(scene_sum, 2)} 秒{extra}与 total_seconds={total} 不一致"
        )
    if len(artifact.get(key, [])) >= 3:
        vals = [round(float(s.get("estimated_seconds") or 0), 2) for s in artifact[key]]
        if len(set(vals)) == 1:
            why = artifact.get("equal_length_justification")
            if not why or len(str(why).strip()) < 15:
                problems.append(
                    f"各场理想时长完全相同（都是 {vals[0]} 秒）：除非这个故事确实需要均匀节奏，"
                    "否则应让场次长短由叙事任务决定；确实需要就写进 equal_length_justification"
                )
    return problems


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 3：大纲 + 正式剧本 + 独立评审（会调用 DeepSeek，产生费用）")
    parser.add_argument("--film-id", required=True)
    parser.add_argument("--concept", help="已批准的概念 id（默认读 state.json 里记录的选择）")
    parser.add_argument("--force", action="store_true", help="已存在成果时归档旧的并重新生成")
    parser.add_argument("--revise", action="store_true",
                        help="按上一份剧本评审的必修项返修（保留现有内容，不重写故事）")
    parser.add_argument("--skip-both-checks", action="store_true", help="跳过大纲校验（调试用，不建议）")
    parser.add_argument("--repairs", type=int, default=make_ideas.DEFAULT_REPAIRS)
    args = parser.parse_args()

    conf = cfg.load_director_config()
    settings = cfg.load_settings()
    films_root = settings.path(conf["film_layout"]["root_key"])
    film_dir = films_root / args.film_id
    if not film_dir.is_dir():
        print(f"[错误] 影片目录不存在：{film_dir}")
        print("       下一步：先跑阶段 2（make_ideas.py）建立影片与提案。")
        return 2

    state = load_state(film_dir, conf)
    require_gate_approved(state, "A", conf)          # 真实停机点
    concept_id = args.concept or str(
        ((state.get("gates") or {}).get("A") or {}).get("chosen_concept") or ""
    )
    if not concept_id:
        print("[停止] 关卡 A 已批准，但没有记录选了哪个概念。")
        print("       下一步：重跑本命令并加 --concept c2（把选择写进 state.json）。")
        return 2

    brief = json.loads((film_dir / "brief.json").read_text(encoding="utf-8"))
    ideas = json.loads((film_dir / "ideas.json").read_text(encoding="utf-8"))
    nominal = nominal_seconds_of(brief, conf)
    concept = find_concept(ideas, concept_id)
    review_path = film_dir / "idea_review.json"
    idea_review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else None
    fixes = review_fixes(idea_review, concept_id, conf)
    print(f"影片 {args.film_id}　已批准概念 {concept_id}《{concept['title']}》"
          f"　成片 {nominal_seconds_of(brief, conf)} 秒")
    print(f"带入评审修法 {len(fixes)} 条：")
    for item in fixes:
        print(f"      - {item}")

    if args.revise:
        return run_revision(args, conf, settings, film_dir, brief, concept, concept_id, state)

    outline_path = film_dir / "story_outline.json"
    screenplay_path = film_dir / "screenplay.json"
    review_json = film_dir / "screenplay_review.json"
    outline_schema = make_ideas.load_schema("story_outline.schema.json")
    screenplay_schema = make_ideas.load_schema("screenplay.schema.json")
    review_schema = make_ideas.load_schema("artifact_review.schema.json")
    duration_check = (None if args.skip_both_checks
                      else (lambda d: duration_problems(d, nominal, conf)))

    # 阶段复用：三份产物各自"通过就复用"，只补缺的那一段（用户 2026-10-15 要求）。
    # 例：剧本已通过、只有评审那次失败 → 重跑本命令只重做评审，不再重写剧本。
    outline = None if args.force else make_ideas.reusable_artifact(
        outline_path, [lambda d: make_ideas.schema_errors(d, outline_schema), duration_check,
                       make_ideas.upstream_guard("concept_id", concept_id)],
        what="故事大纲")
    screenplay = None if args.force else make_ideas.reusable_artifact(
        screenplay_path, [lambda d: make_ideas.schema_errors(d, screenplay_schema), duration_check,
                          make_ideas.upstream_guard("concept_id", concept_id)],
        what="正式剧本")
    review = None if args.force else make_ideas.reusable_artifact(
        review_json, lambda d: make_ideas.schema_errors(d, review_schema), what="剧本评审")
    if outline is not None and screenplay is not None and review is not None:
        print()
        print("完成：本次没有调用 DeepSeek（大纲／剧本／剧本评审全部复用已有成果）。")
        print(f"      {make_ideas.convergence_verdict(review, conf)}")
        print(f"请先看：{film_dir / 'story_outline.md'} ／ {film_dir / 'screenplay.md'} ／ "
              f"{film_dir / 'screenplay_review.md'}")
        print("下一步：要按评审返修加 --revise；要整段重做加 --force（旧文件进 history/）。")
        return 0

    # 记录选择，便于复现
    gates = state.setdefault("gates", {})
    gates.setdefault("A", {})["chosen_concept"] = concept_id
    gates["A"]["chosen_concept_title"] = concept["title"]
    (film_dir / str(conf["film_layout"]["state_file"])).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    client = ds.DeepSeekClient(settings)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calls = 0

    if outline is None:
        print("[1/3] 生成故事大纲…")
        try:
            outline, notes = make_ideas.call_json(
                client,
                system=(DIRECTOR_ROOT / "prompts" / "02_story_outline.md").read_text(encoding="utf-8"),
                user=outline_user_prompt(brief, concept, fixes, conf, outline_schema),
                schema=outline_schema,
                temperature=0.7,
                repairs=args.repairs,
                label="故事大纲",
                extra_check=duration_check,
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 大纲生成失败：{exc}")
            print("       下一步：确认密钥与网络后重跑；失败的调用同样可能计费。")
            return 1
        outline["film_id"] = args.film_id
        outline["concept_id"] = concept_id
        for note in notes:
            print(f"      {note}")
        make_ideas.archive_if_exists(outline_path)   # 旧版（含校验没过的那份）一律先归档
        outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (film_dir / "story_outline.md").write_text(
            render_outline_md(outline, {"created_at": created_at, "model": client.model}),
            encoding="utf-8"
        )
        print(f"      已写入 story_outline.json / .md（{len(outline['scenes'])} 场，"
              f"{len(outline['beats'])} 个节拍）")

    if screenplay is None:
        print("[2/3] 写正式剧本…")
        try:
            screenplay, notes2 = make_ideas.call_json(
                client,
                system=(DIRECTOR_ROOT / "prompts" / "03_screenplay.md").read_text(encoding="utf-8"),
                user=screenplay_user_prompt(brief, concept, outline, fixes, conf, screenplay_schema),
                schema=screenplay_schema,
                temperature=0.6,
                repairs=args.repairs,
                label="正式剧本",
                extra_check=duration_check,
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 剧本生成失败：{exc}")
            print("       大纲已经保存好了；下一步：修好上面的原因后重跑本命令"
                  "（大纲会被自动复用，不用加 --force）。")
            return 1
        screenplay["film_id"] = args.film_id
        screenplay["concept_id"] = concept_id
        for note in notes2:
            print(f"      {note}")
        make_ideas.archive_if_exists(screenplay_path)
        screenplay_path.write_text(json.dumps(screenplay, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        (film_dir / "screenplay.md").write_text(
            render_screenplay_md(screenplay, {"created_at": created_at, "model": client.model}),
            encoding="utf-8"
        )
        print(f"      已写入 screenplay.json / .md（{len(screenplay['scenes'])} 场，"
              f"审核清单 {len(screenplay['review_checklist'])} 条）")

    if review is None:
        print("[3/3] 独立评审剧本…")
        try:
            review, notes3 = make_ideas.call_json(
                client,
                system=(DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8"),
                user=review_user_prompt(screenplay, conf, concept, review_schema, brief=brief),
                schema=review_schema,
                temperature=0.3,
                repairs=args.repairs,
                label="剧本评审",
                extra_check=make_ideas.must_fix_scope_problems,
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 剧本评审失败：{exc}")
            print("       大纲与剧本都已保存；下一步：修好原因后重跑本命令——"
                  "大纲与剧本会被自动复用，只重跑这次失败的评审（不用加 --force）。")
            return 1
        review["film_id"] = args.film_id
        for note in notes3:
            print(f"      {note}")
        make_ideas.archive_if_exists(review_json)
        review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        (film_dir / "screenplay_review.md").write_text(
            render_review_md(review, {"created_at": created_at, "model": client.model}),
            encoding="utf-8"
        )
    print(f"      已写入 screenplay_review.json / .md（必修项 "
          f"{len(review['must_fix_before_next_stage'])} 条）")
    print(f"      {make_ideas.convergence_verdict(review, conf)}")

    # 本阶段属于"制作前确认"这一组，不再单独设闸门：
    # 整组（剧本＋分镜＋提示词）做完后由 make_prompts.py 一次把关卡 B 置为待确认。

    print()
    print(f"完成：本次共调用 DeepSeek {calls} 次（产生费用）。")
    print(f"请先看：{film_dir / 'story_outline.md'}")
    print(f"        {film_dir / 'screenplay.md'}")
    print(f"        {film_dir / 'screenplay_review.md'}")
    print("关卡 B 现在是 PENDING_HUMAN_REVIEW；未批准之前不得进入分镜阶段。")
    return 0


def run_revision(args, conf, settings, film_dir: Path, brief: dict, concept: dict,
                 concept_id: str, state: dict) -> int:
    """按上一次剧本评审的必修项返修大纲与剧本，然后重新做独立评审。

    旧版本一律归档到 history/（不覆盖已审核版本），新版本带 `revision` 字段说明落实了哪几条。
    """
    outline_path = film_dir / "story_outline.json"
    screenplay_path = film_dir / "screenplay.json"
    review_path = film_dir / "screenplay_review.json"
    missing = [p.name for p in (outline_path, screenplay_path, review_path) if not p.is_file()]
    if missing:
        print(f"[错误] --revise 需要已有的第 1 版成果，缺少：{'、'.join(missing)}")
        print("       下一步：先跑一次 make_story.py 生成第一版，再用 --revise 返修。")
        return 2

    old_review = json.loads(review_path.read_text(encoding="utf-8"))
    must_fixes = must_fix_texts(old_review)
    if not must_fixes:
        print("[停止] 上一份评审没有必修项，没有可返修的内容。")
        print("       下一步：直接人工审阅（review_gate.py --approve B 或 --reject B）。")
        return 2
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    screenplay = json.loads(screenplay_path.read_text(encoding="utf-8"))
    revision = int((screenplay.get("revision") or {}).get("revision") or 1) + 1
    issues = issues_text(old_review)
    # 时长校验的"简报规模"：返修同样是按叙事分配、落在 ±容差内即可（不改分镜阶段的映射规则）。
    nominal = nominal_seconds_of(brief, conf)

    outline_schema = make_ideas.load_schema("story_outline.schema.json")
    screenplay_schema = make_ideas.load_schema("screenplay.schema.json")
    review_schema = make_ideas.load_schema("artifact_review.schema.json")
    client = ds.DeepSeekClient(settings)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calls = 0

    print(f"返修：第 {revision - 1} 版 → 第 {revision} 版，共 {len(must_fixes)} 条必修项")
    for index, item in enumerate(must_fixes, 1):
        print(f"      必修{index}：{item[:66]}")

    print(f"[1/3] 修订大纲（第 {revision} 版）…")
    try:
        new_outline, notes = make_ideas.call_json(
            client,
            system=(DIRECTOR_ROOT / "prompts" / "02_story_outline.md").read_text(encoding="utf-8"),
            user=outline_revision_prompt(
                brief, concept, conf, outline, must_fixes, issues, revision, outline_schema
            ),
            schema=outline_schema,
            temperature=0.5,
            repairs=args.repairs,
            label="大纲修订",
            extra_check=lambda d: revision_checks(d, nominal, conf, len(must_fixes)),
        )
        calls += 1
    except (ds.DeepSeekError, RuntimeError) as exc:
        print(f"[错误] 大纲修订失败：{exc}")
        print("       下一步：修好原因后重跑；失败的调用同样可能计费。")
        return 1
    new_outline["film_id"] = args.film_id
    new_outline["concept_id"] = concept_id
    for note in notes:
        print(f"      {note}")
    make_ideas.archive_if_exists(outline_path)
    outline_path.write_text(json.dumps(new_outline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (film_dir / "story_outline.md").write_text(
        render_outline_md(new_outline, {"created_at": created_at, "model": client.model}), encoding="utf-8"
    )
    print(f"      已写入第 {revision} 版 story_outline.json / .md（旧版已归档到 history/）")

    print(f"[2/3] 修订剧本（第 {revision} 版）…")
    try:
        new_screenplay, notes2 = make_ideas.call_json(
            client,
            system=(DIRECTOR_ROOT / "prompts" / "03_screenplay.md").read_text(encoding="utf-8"),
            user=screenplay_revision_prompt(
                brief, concept, conf, new_outline, screenplay, must_fixes, issues, revision, screenplay_schema
            ),
            schema=screenplay_schema,
            temperature=0.5,
            repairs=args.repairs,
            label="剧本修订",
            extra_check=lambda d: revision_checks(d, nominal, conf, len(must_fixes)),
        )
        calls += 1
    except (ds.DeepSeekError, RuntimeError) as exc:
        print(f"[错误] 剧本修订失败：{exc}")
        print("       修订后的大纲已保存；下一步：修好原因后重跑。")
        return 1
    new_screenplay["film_id"] = args.film_id
    new_screenplay["concept_id"] = concept_id
    for note in notes2:
        print(f"      {note}")
    make_ideas.archive_if_exists(screenplay_path)
    screenplay_path.write_text(json.dumps(new_screenplay, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    (film_dir / "screenplay.md").write_text(
        render_screenplay_md(new_screenplay, {"created_at": created_at, "model": client.model}),
        encoding="utf-8",
    )
    print(f"      已写入第 {revision} 版 screenplay.json / .md（旧版已归档到 history/）")

    print(f"[3/3] 重新独立评审（核对 {len(must_fixes)} 条必修项是否真的改到位）…")
    try:
        review, notes3 = make_ideas.call_json(
            client,
            system=(DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8"),
            user=review_user_prompt(new_screenplay, conf, concept, review_schema,
                                    previous=must_fixes, brief=brief),
            schema=review_schema,
            temperature=0.3,
            repairs=args.repairs,
            label="剧本评审",
            extra_check=make_ideas.must_fix_scope_problems,
        )
        calls += 1
    except (ds.DeepSeekError, RuntimeError) as exc:
        print(f"[错误] 剧本评审失败：{exc}")
        print("       修订版大纲与剧本都已保存；下一步：修好原因后重跑。")
        return 1
    review["film_id"] = args.film_id
    for note in notes3:
        print(f"      {note}")
    make_ideas.archive_if_exists(review_path)
    review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (film_dir / "screenplay_review.md").write_text(
        render_review_md(review, {"created_at": created_at, "model": client.model}), encoding="utf-8"
    )
    print(f"      已写入第 {revision} 版 screenplay_review（剩余必修项 "
          f"{len(must_fix_texts(review))} 条）")
    print(f"      {make_ideas.convergence_verdict(review, conf, round_no=revision)}")

    record_gate_b(state, conf, review, revision)
    (film_dir / str(conf["film_layout"]["state_file"])).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"返修完成：本次共调用 DeepSeek {calls} 次（产生费用）。")
    print(f"请先看：{film_dir / 'screenplay.md'}")
    print(f"        {film_dir / 'screenplay_review.md'}")
    print("关卡 B 仍是 PENDING_HUMAN_REVIEW；未批准不得进入分镜阶段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
