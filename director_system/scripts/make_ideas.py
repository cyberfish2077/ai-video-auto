"""阶段 2：创意与故事开发（生成 5 个故事概念 + 一份独立评审）。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe director_system\\scripts\\make_ideas.py ^
        --film-id animal-heads-nap-01 ^
        --brief-file projects\\animal-heads-nap-01\\brief.json

本程序会：

1. 读取创作简报（JSON）与**实测制作约束**（来自导演系统配置与工作流审计）；
2. 调 DeepSeek 生成 5 个差异明确的故事概念，用 ``idea_proposal.schema.json`` 校验
   （不合法就带着错误信息有限次修复）；
3. 用独立评审提示词（``prompts/06_quality_review.md``）再调用一次 DeepSeek，
   产出评审报告并用 ``idea_review.schema.json`` 校验；
4. 写出 ``ideas.json`` / ``ideas.md`` / ``idea_review.json`` / ``idea_review.md``；
5. 建立 ``state.json``，把关卡 A 置为 ``PENDING_HUMAN_REVIEW``（**绝不自动放行**）。

不会做：写剧本、写分镜、提交 ComfyUI、修改工作流。
产出会被 ``--force`` 之外的任何一次调用尊重：已存在的成果默认不覆盖
（旧文件会先归档到 ``history/``）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

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
from video_auto import deepseek as ds  # noqa: E402

#: 每次生成最多修复几轮（第 1 次 + 修复轮）
DEFAULT_REPAIRS = 2

#: 追加到**每一次**生成请求末尾的输出格式硬性要求。
#: 2026-10-14 加：本片简报里写了"开篇沿用上一部片的三段"，模型于是在每个概念里
#: 自己加了一个 schema 里没有的字段 `note_on_reuse`，第一次生成直接被机械校验判死；
#: 随后两轮修复又把 JSON 写坏（见 data/cache/llm_raw/ 的落盘原文）。
#: 这三条把"能做什么、不能做什么"写在离模型最近的地方，并统一要求字符串完整。
FORMAT_RULES = """

【输出格式（硬性；机械校验会照这几条检查）】
1. 只输出上面 schema 里列出的字段——顶层与每一个子对象都是如此：一个都不许多、一个都不许少。
   尤其**不要新增 schema 里没有的字段**（例如 `note_on_reuse`、`segment_plan`、`notes`、`reason`），
   也不要写 `$schema`／`$id`／`title`／`description` 这类只用于说明的键。
2. 只输出一个 JSON 对象：不要代码围栏（```），不要在 JSON 前后写任何解释文字。
3. 每个字符串都要完整闭合：字符串内部要用引号时用中文引号「」或""，**不要**用未转义的半角双引号。
4. 结构必须闭合：每个 `{` 都有配对的 `}`，每个 `[` 都有配对的 `]`；整份输出的开头是**一个** `{`，
   结尾是**唯一一个** `}`。2026-10-15 实测：两次作废都是结尾/中段括号不配对
   （一次少一个闭合括号、一次多写一个 `}`），`finish_reason` 都是 stop、不是截断。
   输出前请从最后一行往前把嵌套层级核对一遍。
5. 数组元素之间用逗号分隔，**最后一个元素后面不要再写逗号**；键与值之间用冒号。
"""


def dump_raw(client: "ds.DeepSeekClient", label: str, attempt: int, raw: str) -> None:
    """把原始回复落盘，便于事后诊断（只在环境变量 ``VIDEO_AUTO_LLM_RAW=1`` 时写）。

    为什么需要：schema 不通过时，光看"回复不是合法 JSON"分不清三种情况——
    被 max_tokens 截断、少写逗号、还是多写了字段。落盘原文 + ``finish_reason``
    能把这三者一次分清，避免"再试一次还是同样失败"地白烧调用。
    """
    if os.environ.get("VIDEO_AUTO_LLM_RAW", "").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        out_dir = client.settings.path("paths.cache") / "llm_raw"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{stamp}-{label}-第{attempt}次.json"
        path.write_text(json.dumps({
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "label": label,
            "attempt": attempt,
            "model": getattr(client, "model", None),
            "finish_reason": getattr(client, "last_finish_reason", None),
            "usage": getattr(client, "last_usage", {}),
            "chars": len(raw),
            "raw": raw,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"      [诊断] 原始回复已落盘：{path}")
    except Exception as exc:  # noqa: BLE001  诊断不能反过来把流程弄挂
        print(f"      [诊断] 原始回复落盘失败（不影响流程）：{exc}")


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def extract_json(text: str) -> object:
    """从模型回复里取出 JSON：容忍代码围栏与前后废话。"""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for candidate in _tolerant_json_fixes(cleaned):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise ValueError("回复里找不到可解析的 JSON 对象")


def _tolerant_json_fixes(text: str) -> list[str]:
    """几处**保守**的 JSON 容错改写（只在严格解析失败后才用）。

    实测（2026-10-14，分镜阶段连败 5 次）模型最常见的两种笔误：
    多一个引号（`]""`）与多余的尾逗号（`,"}`）。这两类改写是幂等的、不会改变语义，
    所以放在严格解析之后做一次；改写不成功就照原样报错，不做更激进的猜测
    （缺 `}`／缺逗号需要括号配平，属于结构性修复，宁可让模型重写）。
    """
    fixes: list[str] = []
    # ① 多打一个引号：`]""` / `}""` → `]` / `}`
    stray = re.sub(r'([}\]])""', r"\1", text)
    if stray != text:
        fixes.append(stray)
    # ② 尾逗号：`,}` / `,]` → `}` / `]`
    for base in ([stray] if stray != text else []) + [text]:
        trimmed = re.sub(r",(\s*[}\]])", r"\1", base)
        if trimmed != base:
            fixes.append(trimmed)
    return [item for item in dict.fromkeys(fixes) if item != text]


def _resolve_ref(schema_node: object, root: dict) -> dict:
    """展开 schema 里的 ``$ref``（只支持 ``#/$defs/x`` 这种本地引用）。"""
    if not isinstance(schema_node, dict):
        return {}
    ref = schema_node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node: object = root
        for part in ref[2:].split("/"):
            if not isinstance(node, dict):
                return {}
            node = node.get(part)
        return node if isinstance(node, dict) else {}
    return schema_node


def prune_unknown(instance: object, schema: dict, *, root: dict | None = None,
                  path: str = "") -> list[str]:
    """按 schema 剔除"schema 之外的字段"，返回被剔除的字段路径清单。

    为什么由程序做：schema 是权威（`additionalProperties: false`），而模型偶尔会多写一个键
    （实测出现过 `space_plan_note`、`shots_count_note`、`risk_ffeags_placeholder`、顶层 `prompt`）。
    这类失败与作品内容无关、纯属输出卫生，却会让整轮生成作废并白烧一次调用。
    剔除**不静默**：键名会记进 notes，报告里能看到。
    """
    root = root if root is not None else schema
    node = _resolve_ref(schema, root)
    dropped: list[str] = []
    if isinstance(instance, dict):
        props = node.get("properties") or {}
        if props and node.get("additionalProperties") is False:
            for key in list(instance.keys()):
                if key not in props:
                    instance.pop(key)
                    dropped.append(f"{path}{key}")
        for key, value in list(instance.items()):
            sub = props.get(key)
            if sub is None:
                continue
            dropped.extend(prune_unknown(value, _resolve_ref(sub, root), root=root,
                                          path=f"{path}{key}."))
    elif isinstance(instance, list):
        items = _resolve_ref(node.get("items") or {}, root)
        for index, item in enumerate(instance):
            dropped.extend(prune_unknown(item, items, root=root, path=f"{path}{index}."))
    return dropped


def load_schema(name: str) -> dict:
    path = cfg.DIRECTOR_SCHEMAS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"找不到 schema：{path}\n解决办法：确认 director_system/schemas/ 下该文件存在。")
    return json.loads(path.read_text(encoding="utf-8"))


def schema_errors(instance: object, schema: dict) -> list[str]:
    """返回人类可读的校验错误（空列表 = 通过）。"""
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in err.path) or '(根)'}：{err.message}"
        for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    ]


#: schema 摘要里把正则写成"形如什么"的人话（模型更容易照做，也少一次看不懂）
_PATTERN_HINTS = {
    r"^SC[0-9]{2}$": "形如 SC01",
    r"^SH[0-9]{2}$": "形如 SH01",
    r"^B[0-9]{2}$": "形如 B01",
    r"^s[1-9]$": "形如 s1",
    r"^c[1-9]$": "形如 c1",
    r"^@图片[1-9]$": "形如 @图片1",
}

_TYPE_NAMES = {
    "string": "字符串", "number": "数字", "integer": "整数",
    "boolean": "布尔", "object": "对象", "array": "数组", "null": "空",
}


def _type_hint(node: dict, root: dict) -> str:
    """把一个标量节点概括成一句人话（类型 + 取值约束）。"""
    node = _resolve_ref(node, root)
    parts: list[str] = []
    declared = node.get("type")
    types = declared if isinstance(declared, list) else [declared]
    names = [_TYPE_NAMES.get(t, str(t)) for t in types if t]
    if names:
        parts.append("/".join(names))
    if "const" in node:
        parts.append(f"固定为 {json.dumps(node['const'], ensure_ascii=False)}")
    if node.get("enum"):
        parts.append("只能是：" + "、".join(json.dumps(v, ensure_ascii=False) for v in node["enum"]))
    if node.get("pattern"):
        parts.append(_PATTERN_HINTS.get(node["pattern"], f"需匹配 {node['pattern']}"))
    if node.get("minimum") is not None and node.get("maximum") is not None:
        parts.append(f"{node['minimum']}~{node['maximum']}")
    elif node.get("minimum") is not None:
        parts.append(f"不小于 {node['minimum']}")
    elif node.get("maximum") is not None:
        parts.append(f"不大于 {node['maximum']}")
    if node.get("minItems") is not None and node.get("maxItems") is not None:
        parts.append(f"{node['minItems']}~{node['maxItems']} 项")
    elif node.get("minItems") is not None:
        parts.append(f"至少 {node['minItems']} 项")
    elif node.get("maxItems") is not None:
        parts.append(f"至多 {node['maxItems']} 项")
    if node.get("minLength") is not None or node.get("maxLength") is not None:
        lo, hi = node.get("minLength"), node.get("maxLength")
        if lo is not None and hi is not None:
            parts.append(f"{lo}~{hi} 个字符")
        elif lo is not None:
            parts.append(f"至少 {lo} 个字符")
        else:
            parts.append(f"至多 {hi} 个字符")
    return "<" + "，".join(parts) + ">" if parts else "<值>"


def _digest(node: object, root: dict) -> object:
    """递归把 schema 节点压成"同样结构、但值只是类型说明"的骨架。

    约定（写在 :data:`SCHEMA_SUMMARY_LEGEND` 里给模型看）：**必填字段的键名末尾加 `*`**，
    数组的键名再带 `[至少~至多 项]`，值用 `[ ... ]` 表示数组（里面是一个元素的样子），
    `< ... >` 是取值说明。
    """
    node = _resolve_ref(node, root)
    if isinstance(node, dict) and (node.get("properties") or node.get("type") == "object"):
        props = node.get("properties") or {}
        need = set(node.get("required") or [])
        return {_key_label(name, sub, root, name in need): _digest(sub, root)
                for name, sub in props.items()}
    if isinstance(node, dict) and (node.get("items") is not None or node.get("type") == "array"):
        return [_digest(node.get("items") or {}, root)]
    return _type_hint(node if isinstance(node, dict) else {}, root)


def _key_label(name: str, sub: object, root: dict, required: bool) -> str:
    """给键名加上标注：`*` = 必填，`[n~m 项]` = 数组元素个数要求。"""
    label = name + ("*" if required else "")
    node = _resolve_ref(sub, root)
    if isinstance(node, dict):
        lo, hi = node.get("minItems"), node.get("maxItems")
        if lo is not None or hi is not None:
            label += f"[{lo if lo is not None else 1}~{hi if hi is not None else '不限'} 项]"
    return label


#: 摆在字段骨架前面给模型看的"读法说明"（三段提示词与提示词编译共用同一份口径）
SCHEMA_SUMMARY_LEGEND = """\
【字段骨架怎么读】下面给的是**结构骨架**，只列字段名、类型与取值约束，**不是内容**：
* 键名末尾带 `*` 的表示**必填**——你写 JSON 时**不要**把 `*` 写进键名；没有 `*` 的字段可以省略。
* 键名末尾带 `[n~m 项]` 的表示**这个数组要几个元素**（例如 `scenes*[2~6 项]`：2~6 场）。
* `[ ... ]` 表示数组，里面描述的是**一个元素**的样子；`{ ... }` 是对象（键名必须与骨架一致）。
* `< ... >` 是"这里该填什么"的说明（类型/取值范围），真实取值由你自己写，**不要照抄**这些说明。
* 键名一个都不许多、一个都不许改名（schema 禁止多余字段，多写的会被剔除并记账）。"""


def schema_summary(schema: dict) -> str:
    """把 JSON Schema 压成**只含字段名/类型/必填**的紧凑骨架（供提示词用）。

    为什么要压（2026-10-15 实测）：快速出片通道首次真跑时，剧本提示词约 11,000 字里
    **近 9,000 字是 schema 的 `description` 全文**，DeepSeek 连续两次在**尾部结构**上翻车
    （一次少一个闭合括号、一次多一个 `}` 导致 Extra data；两次 `finish_reason` 都是 stop，
    不是截断）。把说明文字换成结构骨架后，模型要盯的"结构"变短，机械校验仍然用**完整 schema**
    （`schema_errors`），所以产出的规格一点没放松。

    返回的是**合法 JSON**：字段名（键）与真实结构一致，值一律是 `<类型，必填/可选，取值约束>`
    这种"说明性的假值"，不会被误当成内容照抄。
    """
    return json.dumps(_digest(schema, schema), ensure_ascii=False, indent=2)


def legal_frame_table(conf: dict) -> list[tuple[int, float]]:
    """工作流真正能生成的 (帧数, 秒) 表。

    注意：**帧数是权威值**，秒只是给人看的换算结果。不要用"四舍五入后的秒 × 24"
    反推帧数——那会得到 361 而不是 362（15.0833 秒四舍五入成 15.08 就会这样）。
    """
    gen = conf["generation"]
    frames, step, mod = gen["frames"], gen["frames"]["step"], gen["frames"]["min_mod"]
    table: list[tuple[int, float]] = []
    k = 0
    while True:
        n = step * k + mod
        if n > frames["max"]:
            break
        if n >= frames["min"]:
            table.append((n, round(n / gen["fps"], 2)))
        k += 1
    return table


def legal_seconds(conf: dict) -> list[float]:
    """只要秒数的合法取值（给"最近的合法值"这类提示用）。"""
    return [seconds for _, seconds in legal_frame_table(conf)]


def legal_table_text(conf: dict) -> str:
    return "、".join(f"{seconds} 秒（{frames} 帧）" for frames, seconds in legal_frame_table(conf))


def call_json(
    client: "ds.DeepSeekClient",
    *,
    system: str,
    user: str,
    schema: dict,
    temperature: float,
    repairs: int = DEFAULT_REPAIRS,
    label: str = "输出",
    extra_check: "Callable[[dict], list[str]] | None" = None,
) -> tuple[dict, list[str]]:
    """调用 DeepSeek 并强制 JSON + schema 合规；失败时做有限次修复。"""
    notes: list[str] = []
    user_effective = user + FORMAT_RULES
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_effective},
    ]
    last_problems: list[str] = []
    for attempt in range(1, repairs + 2):
        raw = client.chat(messages, json_mode=True, temperature=temperature)
        dump_raw(client, label, attempt, raw)
        finish = getattr(client, "last_finish_reason", None)
        try:
            data = extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            last_problems = [
                f"回复不是合法 JSON：{exc}"
                f"（本次回复 {len(raw)} 字符，finish_reason={finish}）"
            ]
            if finish == "length":
                last_problems.append(
                    "finish_reason=length：回复被输出上限截断，请压缩文字量后重新输出完整 JSON"
                )
            notes.append(f"{label} 第 {attempt} 次：{last_problems[0]}")
        else:
            if isinstance(data, dict):
                dropped = prune_unknown(data, schema)
                if dropped:
                    notes.append(
                        f"{label} 第 {attempt} 次：已按 schema 剔除 {len(dropped)} 个多余字段"
                        f"（{('、'.join(dropped[:6]))}）"
                    )
                last_problems = schema_errors(data, schema)
                if not last_problems and extra_check is not None:
                    last_problems = extra_check(data)
            else:
                last_problems = [f"顶层必须是 JSON 对象，实际是 {type(data).__name__}"]
            if not last_problems:
                notes.append(f"{label} 第 {attempt} 次：通过 schema 校验")
                return data, notes  # type: ignore[return-value]
            notes.append(f"{label} 第 {attempt} 次：schema 未通过（{len(last_problems)} 处，"
                         f"回复 {len(raw)} 字符，finish_reason={finish}）："
                         + "；".join(last_problems[:4]))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_effective},
            {"role": "assistant", "content": raw[:6000]},
            {
                "role": "user",
                "content": (
                    "上面的回复没有通过校验。请**只输出**修正后的 JSON 对象，不要解释、不要代码围栏。\n"
                    "必须修正的问题：\n- " + "\n- ".join(last_problems[:12])
                ),
            },
        ]
    raise RuntimeError(f"{label}连续 {repairs + 1} 次都不合法：\n" + "\n".join(notes))


# ----------------------------------------------------------------------
# 提示词拼装
# ----------------------------------------------------------------------


def film_scale(conf: dict, brief: dict | None = None) -> tuple[float, float, int, int]:
    """从简报换算出（成片规模秒数、容差、最少段数、最多段数）。

    时长**不写死**：取简报 ``from_user.duration_seconds``，容差取 director.yaml 的
    ``duration_policy.tolerance_seconds``；段数范围按单段合法时长（5.17~15.08 秒）换算。
    2026-10-14 修：这里原来写死"成片 30 秒、拆 2~3 段"（上一部片的遗留），
    与本片简报的 120 秒冲突，独立评审据此报了一条"致命：时长不可行"。
    """
    policy = conf.get("duration_policy") or {}
    brief = brief or {}
    nominal = float((brief.get("from_user") or {}).get("duration_seconds")
                    or policy.get("nominal_seconds") or 30)
    tolerance = float(policy.get("tolerance_seconds") or 1.5)
    gen = conf["generation"]
    fps = gen["fps"]
    longest = gen["frames"]["max"] / fps
    shortest = gen["frames"]["min"] / fps
    min_segments = int(math.ceil((nominal - tolerance) / longest - 1e-9))
    max_segments = int(math.floor((nominal + tolerance) / shortest + 1e-9))
    return nominal, tolerance, max(min_segments, 1), max(max_segments, 1)


def review_severity_counts(review: dict) -> dict[str, int]:
    """按严重度统计评审的问题条目（致命／严重／轻微）。"""
    counts = {"致命": 0, "严重": 0, "轻微": 0}
    for issue in (review or {}).get("issues") or []:
        severity = str(issue.get("severity") or "")
        if severity in counts:
            counts[severity] += 1
    return counts


def must_fix_scope_problems(review: dict) -> list[str]:
    """必修项的**口径校验**：只许致命/严重进来（用户 2026-10-15 定）。

    背景（为什么必须由程序管）：`must_fix_before_next_stage` 是自由文本数组，模型可以
    每轮都往里塞 4~8 条——手册 10.3.6 记着剧本返修 5 轮、每轮仍新增 4~8 条（8→5→4→5），
    这就是路线 A 拖时间的主因：返修轮数压不下来，每一轮都是"生成 + 评审"两次调用。

    可机械判定的代理指标：**必修项条数不得超过 issues 里 致命 + 严重 的条数**
    （一条致命/严重最多对应一条必修项）。超了就判为问题，让 `call_json` 带着
    这条反馈做一轮有限返修（提示词与 schema 里也已写明口径，这里是兜底）。
    """
    counts = review_severity_counts(review)
    limit = counts["致命"] + counts["严重"]
    must_fix = [str(item) for item in (review or {}).get("must_fix_before_next_stage") or []]
    if len(must_fix) <= limit:
        return []
    return [
        f"must_fix_before_next_stage 列了 {len(must_fix)} 条，但 issues 里只有 "
        f"{counts['致命']} 条致命 + {counts['严重']} 条严重（最多允许 {limit} 条）。"
        "请把凡是属于**轻微**的条目从 must_fix 里删掉（它们只留在 issues 里存档，"
        "不阻塞下一阶段），并删掉上一轮已经改到位的条目；只保留真正会挡住进度的硬项"
    ]


def convergence_verdict(review: dict, conf: dict, *, round_no: int | None = None) -> str:
    """按 director.yaml 的 ``review_convergence`` 判据给出"能不能放行"的结论。

    只给建议，**不自动放行**——批准永远是人写进 state.json 的。
    背景：本片剧本返修 5 轮，每轮评审都能再新找出 4~8 条（8→5→4→5）；追"0 条"是无限循环。
    """
    rule = conf.get("review_convergence") or {}
    counts = review_severity_counts(review)
    remaining = len((review or {}).get("must_fix_before_next_stage") or [])
    fatal_limit = int(rule.get("fatal_threshold") or 0)
    serious_limit = int(rule.get("serious_threshold") or 0)
    max_rounds = int(rule.get("max_revision_rounds") or 0)
    head = (f"评审结论：致命 {counts['致命']} / 严重 {counts['严重']} / 轻微 {counts['轻微']}，"
            f"必修项 {remaining} 条")
    if counts["致命"] <= fatal_limit and counts["严重"] <= serious_limit:
        return (f"{head} → 按判据**可以放行**：残余必修项随产物带进下一阶段，并逐条写进闸门备注。")
    if round_no is not None and round_no >= max_rounds:
        return (f"{head} → 已达返修轮次上限（{max_rounds} 轮）：不再自动返修，把残余项交人决定。")
    tail = f"最多 {max_rounds} 轮" if max_rounds else "未设上限"
    return (f"{head} → 建议**再修一轮**（致命必须清零、严重 ≤ {serious_limit}；{tail}）。")


def inherited_opening(brief: dict | None) -> dict | None:
    """简报里声明的"开篇沿用既有素材"（没有就不返回）。

    这是**结构化事实**：写在大纲／剧本／分镜提示词里，避免模型把全片时长都当成新拍内容
    （那会让成片超长，或让机械校验"各场之和 = total_seconds"对不上）。
    """
    block = (brief or {}).get("inherited_opening")
    if not isinstance(block, dict):
        return None
    try:
        seconds = float(block.get("seconds") or 0)
    except (TypeError, ValueError):
        return None
    return block if seconds > 0 else None


def inherited_opening_constraints(brief: dict | None) -> str:
    """把"开篇沿用既有素材"翻译成给大纲／剧本／分镜的硬性约束文本（没有就返回空串）。

    设计要点：沿用段是**既有成片**，本片不重新拍，所以它**不进**场次表／镜头表——
    只用一个片级数字 `inherited_seconds` 计入全片时长。这样"要拍的"与"要登记的"是同一份东西，
    提示词包里也就不会出现"编译了却不提交"的第二套文本。
    """
    block = inherited_opening(brief)
    if block is None:
        return ""
    nominal = float((((brief or {}).get("from_user") or {}).get("duration_seconds")) or 0)
    seconds = float(block["seconds"])
    new_seconds = round(max(nominal - seconds, 0.0), 2)
    segs = "、".join(str(x) for x in (block.get("segment_ids") or []))
    return f"""
【开篇沿用既有素材（硬性事实：不要重写、不要重拍，也不要排进场次/镜头表）】
本片开篇 **{seconds:g} 秒**沿用《{block.get('source_film', '上一部片')}》里已经做好的
{len(block.get('segment_ids') or [])} 个镜头（生成段 {segs}）——它们是**既有成片**：
原样登记，不重新生成、不改时长（每镜 243 帧 = 10.125 秒，24fps）。
所以全片 ≈ {nominal:g} 秒 = 沿用 {seconds:g} 秒 + 新拍约 {new_seconds:g} 秒。
* **不要**为这 30.375 秒写场次或镜头条目（它不拍）；你的场次表／镜头表**只覆盖新拍内容**
  （约 {new_seconds:g} 秒），第一条场次／第一个镜头就是你新拍的第 1 场／第 1 镜；
* 片级字段 `inherited_seconds` 填 **{seconds:g}**；
* `total_seconds` 填**全片**时长 ≈ {nominal:g} 秒＝新拍部分之和 + {seconds:g}；
* 新拍部分按新规则分配时长，长短不必相等；第一个场次／镜头的 continuity_in 要**接住既有成片的
  收束画面**（那一段的落点写在创作简报里），衔接要写成文字，不要靠观众自己回忆。
"""


def production_constraints(conf: dict, brief: dict | None = None) -> str:
    """把实测到的硬约束写清楚——概念必须"能拍得出来"，不是纸上剧本。"""
    gen = conf["generation"]
    frames, res = gen["frames"], gen["resolution"]
    nominal, tolerance, min_seg, max_seg = film_scale(conf, brief)
    return f"""\
【实测制作约束（来自工作流审计与导演系统配置，任何概念都必须满足）】
1. 成片总时长约 {nominal:g} 秒（±{tolerance:g} 秒），从开画到结束。具体每一段多长由分镜按
   戏剧节拍与叙事任务决定——**不许为了凑总时长而平均分段**（规则见 director.yaml 的 pacing_policy）。
2. 单次生成只能是一段**连续镜头**，长度 5.2~15.1 秒
   （24fps，帧数只能取 {frames['step']}k+{frames['min_mod']}，即 {frames['min']}~{frames['max']} 帧）。
   按这个规模换算，全片大约由 {min_seg}~{max_seg} 段生成组成（长短可以差很多），再由后期剪接；
   每段之间靠参考图保持人物与场景一致。
3. 分辨率固定 {res['width']}x{res['height']}（16:9）。
4. 画面里**绝对不能出现任何文字**（字幕、招牌、屏幕、书页、标签都会被画出来且不可控），
   台词有被烧成画面字幕的风险——概念不要把关键信息押在"读到的文字"上。
5. 每段最多 9 张参考图，且每张图必须在提示词里逐张声明用途（角色参考 / 画面参考）。
6. 参考素材**只有图片**：没有实拍视频参考、没有音频参考；负面提示词在该工作流不生效。
7. 人物跨段一致靠参考图（需要先出角色设定图）；同一套服装与造型要全程保持。
8. 复杂长动作、多人快速走位、精细手部特写、镜头内切换，都容易崩；宁可拆段，不要硬塞。
9. **同一段画面里清晰可辨的"动物头"最多 2 个**；其余一律后脑勺、虚焦或剪影。
   参考图预算按"每段 ≤9 张"倒推：角色正面/侧面各 1 张、场景 1~2 张、分镜 1 张，
   所以一段里能重点刻画的角色不要超过 2 个。
"""


def ideas_user_prompt(brief: dict, conf: dict, schema: dict) -> str:
    policy = conf.get("duration_policy") or {}
    nominal = float(policy.get("nominal_seconds") or 30)
    tolerance = float(policy.get("tolerance_seconds") or 1.5)
    return f"""\
下面是**创作简报**与制作约束。请按你的工作说明产出 5 个差异明确的故事概念。

【创作简报（用户提供）】
{json.dumps(brief, ensure_ascii=False, indent=2)}

{production_constraints(conf, brief)}

【本片结构：开篇沿用 + 之后续写】
本片是上一部短片《优化名单》的续篇。**开篇约 30 秒沿用上一部片已经拍好的三个镜头**
（引入异常 → 升级 → 收束，结尾是他俯身凑向一只空杯，暗示"他也在变"）。
那三个镜头属于**既有素材**：不需要你重新设计，也不要为它新增任何字段；
你要设计的是**之后约 90 秒的新内容**——它必须接得住那个结尾（他接任了执行者、
开始给别人别针），并把代价最终推回到他自己身上。
这件事如果影响你的节奏判断，写进 `pacing_intent` 的文字里即可。

【节奏只写意图，不要在创意阶段分段】
**不要在这个阶段分配分段、帧数或每段秒数**——镜头边界与时长由分镜阶段按戏剧节拍决定
（项目规则见 director_system/config/director.yaml 的 pacing_policy：
先定镜头边界 → 再按叙事任务分配时长 → 最后才映射到工作流允许的合法帧数）。
你只需要给 `pacing_intent`：这个方向的**节奏性质与意图**——哪里该停顿、哪里该急促、
有没有必须一口气拍完的长镜头、结尾是否需要一个慢下来的拍子。成片总时长约
{nominal:g} 秒（±{tolerance:g} 秒）只是规模提示，不是要你去凑数。

【差异矩阵（硬性要求，违反则不通过）】
5 个概念不能是同一个母题换五种包装。必须满足：
1. **开场情境**：最多 2 个概念可以写成"他午休醒来发现同事变了"；
   其余概念必须换进入方式（例如变化发生在他清醒时／他自己先变了／他是唯一没变的人／
   变化只在某个条件下可见）。
2. **主角位置**：至少 2 个概念里主角**不是**"唯一没被同化的正常人"。
3. **结局性质**：至少覆盖三类，且各自被前文铺垫兑现——
   （a）付出可见代价；（b）荒诞的接纳或和解；（c）结构性揭露（观众发现真正的规则）。
4. **叙事视角**：至少 1 个概念不是"跟着主角的第一人称式观察"，而是别的组织结构
   （例如全片只有同事的视角、或一个物件的视角、或同一时刻的重复）。
把每条概念满足哪几条写在 `score_evidence` 里。

【输出要求】
严格按下面的 JSON Schema 输出（`$schema`/`$id`/`title`/`description` 只是说明，不必回显）：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
所有文字用**简体中文**。id 依次为 c1~c5。`recommendation.recommended_ids` 恰好两个。
5 个概念必须在"世界观解释"和"结局性质"上有实质差异，不能只换角色名或换场景。
不要用梦境结尾、不要用"其实早就死了"这类廉价反转，不要靠巧合推进。
"""


def review_user_prompt(brief: dict, ideas: dict, conf: dict, schema: dict) -> str:
    nominal, tolerance, min_seg, max_seg = film_scale(conf, brief)
    return f"""\
请评审下面这份**创意提案**（5 个故事概念）。你是独立评审，不是作者，不要替作品辩护，
也不要把分数当结论——每条问题都要指出：哪个概念、哪一类、多严重、证据是什么、最小的修法是什么。

【创作简报】
{json.dumps(brief, ensure_ascii=False, indent=2)}

【待评审的 5 个概念】
{json.dumps(ideas, ensure_ascii=False, indent=2)}

{production_constraints(conf, brief)}

【本次评审必须覆盖的五件事】
1. 套路：哪些是熟悉的既有套路，是否只是把类型元素拼在一起；
2. 因果：事件之间是否真有因果推进，主角是否有能动性（不是被动挨着发生）；
3. 人物动机：主角的目标/阻碍/赌注是否合理可信，选择是否有代价；
4. 视觉可表现性：关键信息是否靠"看得见的画面"而不是靠旁白/文字/台词解释；
5. 结尾公平性：反转是否由前面铺垫兑现，是否对观众公平。
另外必须评估：**本片规模（约 {nominal:g} 秒 ±{tolerance:g} 秒、约 {min_seg}~{max_seg} 段生成）**
这个时限下是否真做得出来。若简报里写明开篇沿用既有素材，则新拍部分只有剩下的时长可用，
必须按剩下的时长来评估可行性。

【输出要求】
严格按下面的 JSON Schema 输出：
```json
{json.dumps(schema, ensure_ascii=False, indent=2)}
```
所有文字用**简体中文**。`status` 必须是 PENDING_HUMAN_REVIEW（不要自动放行）。
`per_concept` 必须覆盖 c1~c5 五个。`issues` 至少 3 条，按严重度排序。
"""


# ----------------------------------------------------------------------
# 人类可读的 Markdown
# ----------------------------------------------------------------------


def render_ideas_md(ideas: dict, brief: dict, meta: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 创意提案：{ideas.get('film_id', '')}")
    lines.append("")
    lines.append(f"- 生成时间：{meta['created_at']}")
    lines.append(f"- 模型：{meta['model']}　调用次数：{meta['calls']}")
    lines.append(f"- 校验：idea_proposal.schema.json 通过（尝试 {meta['attempts']} 次）")
    lines.append(f"- 创作简报摘要：{brief.get('from_user', {}).get('theme', '')}"
                 f"（{brief.get('from_user', {}).get('duration_seconds', '?')} 秒）")
    lines.append("")
    lines.append("> 关卡 A 状态：**PENDING_HUMAN_REVIEW**（等你看完这份提案后决定）")
    lines.append("")
    for c in ideas["concepts"]:
        scores = c["scores"]
        lines.append(f"## {c['id']} · {c['title']}")
        lines.append("")
        lines.append(f"**一句话故事**：{c['logline']}")
        lines.append("")
        p = c["protagonist"]
        lines.append(f"- 主角：{p['who']}")
        lines.append(f"- 他要什么：{p['goal']}")
        lines.append(f"- 什么挡着：{p['obstacle']}")
        lines.append(f"- 输了会怎样：{p['stakes']}")
        lines.append(f"- 核心矛盾：{c['central_contradiction']}")
        lines.append("")
        lines.append("**三个记得住的画面**")
        lines.append("")
        for i, event in enumerate(c["visual_events"], 1):
            lines.append(f"{i}. {event}")
        lines.append("")
        lines.append("**因果升级**")
        lines.append("")
        for step in c["causal_escalation"]:
            lines.append(f"- {step}")
        lines.append("")
        e = c["ending"]
        lines.append(f"**结尾**：{e['what_happens']}")
        lines.append("")
        lines.append(f"（铺垫：{e['how_it_was_set_up']}；是否兑现公平：{'是' if e['is_fair'] else '否'}）")
        lines.append("")
        lines.append(f"- 情绪承诺：{c['emotional_promise']}")
        lines.append(f"- 节奏意图：{c.get('pacing_intent', '（未给）')}")
        lines.append("")
        plan = c.get("segment_plan")          # 首版成果才有；新流程在分镜阶段才分段
        if plan:
            lines.append("> ⚠️ 下面这张分段表来自**首版流程**（创意阶段就把分段锁死，"
                         "导致三段等长）。新流程改为先定镜头边界与叙事时长、最后才映射合法帧数，"
                         "分段表不再出现在创意阶段。")
            lines.append("")
            lines.append(f"- 首版分段：生成总长 {plan['generated_seconds']} 秒"
                         f"（{plan.get('post_trim_note', '')}）")
            lines.append("")
            lines.append("| 段 | 帧数 | 秒 | 内容 |")
            lines.append("| --- | --- | --- | --- |")
            for seg in plan["segments"]:
                lines.append(f"| {seg['id']} | {seg['frames']} | {seg['seconds']} | "
                             f"{seg.get('content', '')} |")
            lines.append("")
        lines.append(f"- 最强的地方：{c['strongest_feature']}")
        lines.append(f"- 最弱的地方：{c['main_weakness']}")
        lines.append("")
        lines.append("**制作风险**")
        lines.append("")
        for risk in c["production_risks"]:
            lines.append(f"- {risk}")
        lines.append("")
        lines.append("| 维度 | 分数 |")
        lines.append("| --- | --- |")
        for key, value in scores.items():
            lines.append(f"| {key} | {value} |")
        lines.append("")
        lines.append(f"打分依据：{c['score_evidence']}")
        lines.append("")
    rec = ideas["recommendation"]
    lines.append("## 作者的推荐")
    lines.append("")
    lines.append(f"- 推荐：{ '、'.join(rec['recommended_ids']) }")
    lines.append(f"- 取舍：{rec['tradeoffs']}")
    lines.append(f"- 给选案人的话：{rec['summary_for_human']}")
    lines.append("")
    return "\n".join(lines)


def render_review_md(review: dict, meta: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 独立评审报告：{review.get('film_id', '')}")
    lines.append("")
    lines.append(f"- 生成时间：{meta['created_at']}")
    lines.append(f"- 模型：{meta['model']}　调用次数：{meta['calls']}")
    lines.append(f"- 校验：idea_review.schema.json 通过（尝试 {meta['attempts']} 次）")
    lines.append(f"- 状态：{review['status']}（评审不自动放行，必须人工批准）")
    lines.append("")
    lines.append(f"## 结论摘要\n\n{review['summary']}")
    lines.append("")
    lines.append("## 逐概念裁决")
    lines.append("")
    lines.append("| 概念 | 裁决 | 理由 |")
    lines.append("| --- | --- | --- |")
    for item in review["per_concept"]:
        lines.append(f"| {item['id']} | {item['verdict']} | {item['reason']} |")
    lines.append("")
    lines.append("## 问题清单（按严重度）")
    lines.append("")
    order = {"致命": 0, "严重": 1, "轻微": 2}
    for issue in sorted(review["issues"], key=lambda i: order.get(i["severity"], 9)):
        lines.append(f"### [{issue['severity']}] {issue['category']}（{issue['scope']}）")
        lines.append("")
        lines.append(f"- 证据：{issue['evidence']}")
        lines.append(f"- 最小修法：{issue['smallest_fix']}")
        lines.append("")
    lines.append("## 套路提醒")
    lines.append("")
    if review["cliche_flags"]:
        for flag in review["cliche_flags"]:
            lines.append(f"- {flag}")
    else:
        lines.append("- 未发现明显套路风险。")
    lines.append("")
    lines.append("## 四项专项评估")
    lines.append("")
    lines.append(f"- 因果：{review['causality_assessment']}")
    lines.append(f"- 人物动机：{review['motivation_assessment']}")
    lines.append(f"- 视觉可表现性：{review['visual_feasibility']}")
    lines.append(f"- 结尾公平性：{review['ending_fairness']}")
    lines.append(f"- 时长可行性（成片规模 vs 工作流单段上限）：{review['duration_feasibility']}")
    lines.append("")
    rec = review["recommendation"]
    lines.append("## 评审的推荐")
    lines.append("")
    lines.append(f"- 首选：{rec['top_pick']}；次选：{rec['runner_up']}")
    lines.append(f"- 理由：{rec['why']}")
    lines.append("")
    lines.append("**必须由人决定的事项**")
    lines.append("")
    for item in rec["what_human_must_decide"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 目录与状态
# ----------------------------------------------------------------------


def ensure_film_dir(films_root: Path, film_id: str, conf: dict) -> Path:
    film_dir = films_root / film_id
    film_dir.mkdir(parents=True, exist_ok=True)
    for name in conf["film_layout"]["subdirs"]:
        (film_dir / name).mkdir(exist_ok=True)
    return film_dir


def archive_if_exists(path: Path) -> None:
    """不覆盖旧成果：旧文件挪进 history/ 后另起一份。"""
    if not path.exists():
        return
    history = path.parent / "history"
    history.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = history / f"{path.stem}.{stamp}{path.suffix}"
    shutil.move(str(path), str(target))
    print(f"     （旧 {path.name} 已归档到 history/{target.name}）")


def upstream_guard(field: str, expected) -> Callable[[dict], list[str]]:
    """复用时的"上游版本一致性"闸门。

    复用一份产物前必须先确认它对应的是**同一版上游**：否则会静默地把一条基于旧剧本的
    分镜（或基于旧分镜的提示词包）当成有效的继续往下走。返回的函数接收产物 dict、返回问题列表。
    """
    def check(data: dict) -> list[str]:
        actual = data.get(field)
        if actual is None:
            return [f"缺少 {field}，无法确认它对应的是哪一版上游（保守起见重新生成）"]
        if str(actual) != str(expected):
            return [f"{field}={actual} 与当前上游（{expected}）不一致（上游改过，需重做）"]
        return []

    return check


def reusable_artifact(path: Path, checks=None, *, what: str = "产物",
                      hint: str = "") -> dict | None:
    """已有产物**通过全部机械校验**就复用它（省掉这一段 DeepSeek 调用）。

    为什么要有（用户 2026-10-15 要求"阶段复用/续跑"）：严格版四个阶段是串行的，
    上一阶段通过、下一阶段失败时，原来的路只有两条——`--force` 整段重做（多烧调用），
    或者停下等人判断。这里把"快速出片"通道验证过的复用逻辑搬到严格版：
    **通过就复用、只补缺的那一段**；读不出来或校验不过才重新生成（并说清原因）。

    ``checks``：一个函数或一串函数，各自接收产物 dict、返回问题列表（空 = 通过）。
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"      已有 {path.name} 读不出来（{type(exc).__name__}）→ 重新生成")
        return None
    funcs = checks if isinstance(checks, (list, tuple)) else [checks]
    problems: list[str] = []
    for func in funcs:
        if func is None:
            continue
        problems += list(func(data))
    if problems:
        print(f"      已有 {path.name} 有 {len(problems)} 处问题 → 重新生成：{problems[0]}")
        return None
    print(f"      复用已有 {path.name}（{what}校验通过，没花调用）{hint}")
    return data


def initial_state(conf: dict, film_id: str, gate_id: str, artifacts: list[str]) -> dict:
    gates: dict[str, dict] = {}
    for gate in conf["review_gates"]:
        status = conf["initial_gate_state"] if gate["id"] == gate_id else "NOT_STARTED"
        entry: dict[str, object] = {
            "title": gate["title"],
            "status": status,
            "unlocks": gate["unlocks"],
        }
        if gate["id"] == gate_id:
            entry["artifacts"] = artifacts
            entry["note"] = "等人工看过提案与评审后，用 review_gate.py 批准或驳回。"
        gates[gate["id"]] = entry
    return {
        "film_id": film_id,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gates": gates,
    }


def write_state(film_dir: Path, conf: dict, film_id: str, gate_id: str, artifacts: list[str]) -> None:
    """建立/更新关卡状态；**已存在的关卡状态绝不被覆盖**（人工决定优先）。"""
    state_path = film_dir / str(conf["film_layout"]["state_file"])
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        entry = state.setdefault("gates", {}).setdefault(gate_id, {})
        if entry.get("status") in (None, "", "NOT_STARTED"):
            entry["status"] = conf["initial_gate_state"]
            entry["unlocks"] = entry.get("unlocks") or ""
        entry["artifacts"] = artifacts
        state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"      state.json 已更新（关卡 {gate_id} 现状：{entry.get('status')}，未被覆盖）")
        return
    state = initial_state(conf, film_id, gate_id, artifacts)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"      state.json 已建立（关卡 {gate_id} = {conf['initial_gate_state']}）")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 2：生成 5 个故事概念 + 独立评审（会调用 DeepSeek，产生费用）")
    parser.add_argument("--film-id", required=True, help="影片 id（会成为 projects/<film_id>/）")
    parser.add_argument("--brief-file", required=True, help="创作简报 JSON 路径")
    parser.add_argument("--force", action="store_true", help="已存在成果时归档旧的并重新生成")
    parser.add_argument("--skip-review", action="store_true", help="只生成提案，不做独立评审（不推荐）")
    parser.add_argument("--repairs", type=int, default=DEFAULT_REPAIRS, help="schema 不合规时的修复轮数")
    args = parser.parse_args()

    conf = cfg.load_director_config()
    settings = cfg.load_settings()
    films_root = settings.path(conf["film_layout"]["root_key"])

    brief_path = Path(args.brief_file)
    if not brief_path.is_absolute():
        brief_path = PROJECT_ROOT / brief_path
    if not brief_path.is_file():
        print(f"[错误] 找不到创作简报：{brief_path}")
        print("       下一步：先写一份 brief.json（题材/片长/风格/受众/情绪/限制/参考素材）。")
        return 2
    brief = json.loads(brief_path.read_text(encoding="utf-8"))

    film_dir = ensure_film_dir(films_root, args.film_id, conf)
    print(f"影片目录：{film_dir}")

    ideas_path = film_dir / "ideas.json"
    idea_schema = load_schema("idea_proposal.schema.json")
    review_schema = load_schema("idea_review.schema.json")
    system_prompt = (DIRECTOR_ROOT / "prompts" / "01_idea_development.md").read_text(encoding="utf-8")
    review_path = film_dir / "idea_review.json"

    # 阶段复用：产物已通过校验就不再花调用；只补缺的那一段（用户 2026-10-15 要求）
    ideas = None if args.force else reusable_artifact(
        ideas_path, [lambda d: schema_errors(d, idea_schema),
                     lambda d: [] if d.get("film_id") in (None, args.film_id)
                     else [f"film_id={d.get('film_id')} 不是本片（{args.film_id}）"]],
        what="创意提案")
    review = None if (args.force or args.skip_review) else reusable_artifact(
        review_path, lambda d: schema_errors(d, review_schema), what="创意评审")
    if ideas is not None and (review is not None or args.skip_review):
        write_state(film_dir, conf, args.film_id, "A",
                    ["brief.json", "ideas.json", "ideas.md"] + (
                        ["idea_review.json", "idea_review.md"] if review is not None else []))
        print()
        print("完成：本次没有调用 DeepSeek（全部复用已有成果）。")
        print(f"请先看人类可读版本：{film_dir / 'ideas.md'}"
              + (f" 与 {film_dir / 'idea_review.md'}" if review is not None else ""))
        print("关卡 A 现在是 PENDING_HUMAN_REVIEW；想重做提案加 --force（旧文件进 history/）。")
        return 0

    client = ds.DeepSeekClient(settings)
    calls = 0
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nominal_seconds = float(
        (brief.get("from_user") or {}).get("duration_seconds")
        or (conf.get("duration_policy") or {}).get("nominal_seconds")
        or 30
    )

    if ideas is None:
        print(f"[1/2] 调用 DeepSeek 生成 5 个故事概念（模型 {client.model}）…")
        try:
            ideas, notes = call_json(
                client,
                system=system_prompt,
                user=ideas_user_prompt(brief, conf, idea_schema),
                schema=idea_schema,
                temperature=0.8,
                repairs=args.repairs,
                label="创意提案",
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 生成失败：{exc}")
            print("       下一步：确认 .env 里的 DEEPSEEK_API_KEY 与网络后重跑本命令；"
                  "失败的调用同样可能计费，所以先看清报错原因再重试。")
            return 1
        for note in notes:
            print(f"      {note}")

        archive_if_exists(ideas_path)   # 旧版（含校验没过的那份）一律先归档，不直接覆盖
        ideas.setdefault("film_id", args.film_id)
        ideas["film_id"] = args.film_id
        meta = {"created_at": created_at, "model": client.model, "calls": calls, "attempts": len(notes)}
        ideas_path.write_text(json.dumps(ideas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (film_dir / "ideas.md").write_text(render_ideas_md(ideas, brief, meta), encoding="utf-8")
        print(f"      已写入 {ideas_path.name} 与 ideas.md（5 个概念，推荐 "
              f"{'、'.join(ideas['recommendation']['recommended_ids'])}）")

    artifacts = ["brief.json", "ideas.json", "ideas.md"]
    if review is not None:
        artifacts += ["idea_review.json", "idea_review.md"]
    elif not args.skip_review:
        review_system = (DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8")
        print("[2/2] 调用 DeepSeek 做独立评审（另一个提示词、另一种角色）…")
        try:
            review, notes2 = call_json(
                client,
                system=review_system,
                user=review_user_prompt(brief, ideas, conf, review_schema),
                schema=review_schema,
                temperature=0.3,
                repairs=args.repairs,
                label="独立评审",
                extra_check=must_fix_scope_problems,
            )
            calls += 1
        except (ds.DeepSeekError, RuntimeError) as exc:
            print(f"[错误] 评审失败：{exc}")
            print("       提案已经保存好了；下一步：修好上面的原因后重跑本命令——"
                  "提案会被自动复用，只重跑这次失败的评审（不用加 --force）。")
            return 1
        for note in notes2:
            print(f"      {note}")
        archive_if_exists(review_path)
        review["film_id"] = args.film_id
        meta2 = {"created_at": created_at, "model": client.model, "calls": calls, "attempts": len(notes2)}
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (film_dir / "idea_review.md").write_text(render_review_md(review, meta2), encoding="utf-8")
        print(f"      已写入 {review_path.name} 与 idea_review.md（首选 {review['recommendation']['top_pick']}）")
        artifacts += ["idea_review.json", "idea_review.md"]

    write_state(film_dir, conf, args.film_id, "A", artifacts)

    print()
    print(f"完成：本次共调用 DeepSeek {calls} 次（产生费用）。")
    print(f"请先看人类可读版本：{film_dir / 'ideas.md'} 与 {film_dir / 'idea_review.md'}")
    print("关卡 A 现在是 PENDING_HUMAN_REVIEW；未批准之前不得进入大纲阶段。")
    print(f"批准方式：.venv\\Scripts\\python.exe director_system\\scripts\\review_gate.py "
          f"--film-id {args.film_id} --approve A --note \"你的意见\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
