"""工作流体检程序（阶段五 第一小步）。

只读分析 ComfyUI 的 UI 格式工作流，回答三个问题：

1. 这份工作流用到的节点，本机 ComfyUI **是否全都有**？
2. 工作流里引用的模型文件 / 素材文件，本机**是否真的存在**？
3. 哪些参数需要由程序在每个片段里替换（提示词、时长、分辨率、种子）？

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe scripts\\inspect_workflow.py
    .venv\\Scripts\\python.exe scripts\\inspect_workflow.py --refresh   # 强制重新拉取节点信息

本程序不提交任何任务、不修改任何文件；只在 data/cache/ 下写一份节点信息缓存。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import requests  # noqa: E402

from video_auto import config as cfg  # noqa: E402
from video_auto import workflow as wf_mod  # noqa: E402

#: 这些类型的输入在界面上是“控件”（下拉框/输入框），会存进 widgets_values
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}

#: ComfyUI 节点 mode 取值：0=正常 2=静音 4=跳过
MODE_NAMES = {0: "正常", 2: "静音", 4: "已跳过"}

#: 只存在于 ComfyUI 前端的节点（后端 object_info 里本来就没有，不算缺失）
FRONTEND_ONLY = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute", "RerouteNode"}


def say(level: str, title: str, detail: str = "") -> None:
    print(f"[{level}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


# --------------------------------------------------------------------------
# 节点信息（object_info）
# --------------------------------------------------------------------------


def combo_options(spec: list) -> list[str]:
    """把输入定义里的“可选值列表”抽出来（兼容新旧两种写法）。"""
    return wf_mod.combo_options(spec)


def input_rows(spec_node: dict) -> list[tuple[str, str, list[str]]]:
    """列出某节点类型的“控件型”输入：(名字, 类别, 可选值)。

    类别 enum = 下拉框（可选值就是合法取值）；其它 = 数字/文本输入框。
    只返回界面上真的会显示成控件的输入项（跳过连线型输入）。
    """
    # 直接复用主程序的实现：两份逻辑各自演化过，曾因此漏检"模型目录为空"
    return wf_mod.widget_rows(spec_node)


def fetch_object_info(base_url: str, cache_file: Path, *, refresh: bool, max_age_hours: int = 12) -> dict:
    if not refresh and cache_file.exists():
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            print(f"（使用缓存 {cache_file.name}，{age_h:.1f} 小时前生成；用 --refresh 强制刷新）")
            return json.loads(cache_file.read_text(encoding="utf-8"))
    url = base_url.rstrip("/") + "/object_info"
    print(f"（正在从 {url} 拉取节点信息，约 6MB，稍等）")
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"（已缓存到 {cache_file}）")
    return data


# --------------------------------------------------------------------------
# 工作流解析
# --------------------------------------------------------------------------


def node_list(workflow: dict) -> list[dict]:
    return sorted(workflow.get("nodes", []), key=lambda n: int(n.get("id", 0)))


def link_inputs(node: dict) -> set[str]:
    """该节点上“被连线占据”的输入名（这些位置的控件值不生效，不用校验）。"""
    names = set()
    for item in node.get("inputs", []) or []:
        if item.get("link") is not None:
            names.add(str(item.get("name", "")))
    return names


def widget_values(node: dict, spec_node: dict) -> dict[str, object]:
    """把 widgets_values 与输入名对应起来。

    ComfyUI 有两种存法：老版本是位置数组，新版本可能是字典。
    位置数组按“控件型输入声明顺序”对齐，个别节点末尾会多出按钮类控件，
    因此这里只对齐能对上的部分，不做强行猜测。
    """
    raw = node.get("widgets_values")
    return wf_mod.map_widgets(spec_node, raw)


def normalize(text: str) -> str:
    """统一路径分隔符，便于比较 ComfyUI 返回值与工作流里写的值。"""
    return str(text).replace("/", "\\").strip()


def check_workflow(workflow: dict, object_info: dict) -> dict:
    """核心体检：缺失节点、缺失文件、素材需求、可参数化项。"""
    nodes = node_list(workflow)
    missing_types: dict[str, list[int]] = {}
    missing_values: list[tuple] = []
    active_inputs: list[tuple] = []
    param_candidates: list[tuple] = []

    for node in nodes:
        ntype = str(node.get("type", ""))
        nid = int(node.get("id", 0))
        mode = int(node.get("mode", 0) or 0)
        spec_node = object_info.get(ntype)

        if spec_node is None:
            if ntype not in FRONTEND_ONLY:
                missing_types.setdefault(ntype, []).append(nid)
            continue

        linked = link_inputs(node)
        values = widget_values(node, spec_node)
        for name, kind, options in input_rows(spec_node):
            if name in linked or kind != "enum":
                continue
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                continue          # 空字符串通常是“留待用户填写”，不算配置错误
            if not options:
                # 可选值为空 = 本机对应目录里没有任何文件，这是硬缺失
                missing_values.append((nid, ntype, name, value, 0, mode))
                continue
            if normalize(value) in {normalize(o) for o in options}:
                continue
            missing_values.append((nid, ntype, name, value, len(options), mode))

        # 素材类节点：LoadImage / VHS_LoadVideo / LoadAudio
        if ntype in ("LoadImage", "VHS_LoadVideo", "LoadAudio") and mode == 0:
            first = next((v for v in values.values() if isinstance(v, str) and v.strip()), "")
            active_inputs.append((nid, ntype, str(node.get("title", "")), str(first)))

    # 可参数化项：会在片段之间变化的输入
    interesting = {
        "PrimitiveStringMultiline": "提示词文本",
        "PrimitiveFloat": "时间长度（秒）",
        "PrimitiveInt": "整数参数",
        "ResolutionSelector": "分辨率",
        "SelfLiftH3Sampler": "种子与采样参数",
        "VHS_VideoCombine": "输出文件名与编码",
    }
    for node in nodes:
        ntype = str(node.get("type", ""))
        if ntype in interesting:
            values = widget_values(node, object_info.get(ntype, {}))
            detail = " | ".join(f"{k}={str(v)[:60]}" for k, v in values.items())
            param_candidates.append((int(node.get("id", 0)), ntype, interesting[ntype], detail))

    return {
        "nodes": nodes,
        "missing_types": missing_types,
        "missing_values": missing_values,
        "active_inputs": active_inputs,
        "params": param_candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ComfyUI 工作流体检（阶段五）")
    parser.add_argument("--refresh", action="store_true", help="强制重新拉取节点信息")
    parser.add_argument("--workflow", help="要体检的工作流文件路径（默认读配置里的主力工作流）")
    parser.add_argument("--brief", action="store_true", help="只输出一屏摘要，便于批量比较多个工作流")
    args = parser.parse_args()

    say("OK", "开始工作流体检")
    try:
        settings = cfg.load_settings()
        workflow_path = (
            Path(args.workflow) if args.workflow else settings.path("comfyui.main_workflow", must_exist=True)
        )
        if not workflow_path.exists():
            raise cfg.ConfigError(f"找不到工作流文件：{workflow_path}")
        base_url = str(settings.get("comfyui.base_url"))
        input_dir = settings.path("comfyui.input_dir")
        cache_file = settings.path("paths.cache") / "object_info_cache.json"
    except cfg.ConfigError as exc:
        print(f"[错误] {exc}")
        return 2

    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    try:
        object_info = fetch_object_info(base_url, cache_file, refresh=args.refresh)
    except Exception as exc:  # noqa: BLE001
        say("错误", "无法从 ComfyUI 获取节点信息",
            f"{exc}\n解决办法：确认 ComfyUI 已启动，且 {base_url} 可访问。")
        return 2

    result = check_workflow(workflow, object_info)

    blocking_values = [row for row in result["missing_values"] if row[5] == 0]
    hard_errors = len(result["missing_types"]) + len(blocking_values)

    if args.brief:
        print(f"文件：{workflow_path.name}")
        print(f"  格式：{'UI' if 'nodes' in workflow else 'API'}   "
              f"节点 {len(result['nodes'])} 个 / 类型 {len({n.get('type') for n in result['nodes']})} 种")
        print(f"  缺失节点类型：{len(result['missing_types'])} 个"
              + (f" -> {', '.join(sorted(result['missing_types']))}" if result["missing_types"] else ""))
        print(f"  缺失模型文件：{len(blocking_values)} 个"
              + (f" -> {', '.join(sorted({r[3] for r in blocking_values}))}" if blocking_values else ""))
        missing_assets = [a for a in result["active_inputs"] if not (input_dir / a[3]).exists()]
        print(f"  缺失素材文件：{len(missing_assets)} / {len(result['active_inputs'])} 个启用中的素材节点")
        print(f"  结论：{'❌ 现在跑不通（缺 ' + str(hard_errors) + ' 处）' if hard_errors else '✅ 可以运行'}")
        return 1 if hard_errors else 0

    # ---------------- 1. 概览 ----------------
    section("1. 工作流概览")
    is_ui_format = "nodes" in workflow and "links" in workflow
    print(f"文件：{workflow_path}")
    print(f"格式：{'UI 格式（界面用，需转换后才能通过 API 提交）' if is_ui_format else 'API 格式（可直接提交）'}")
    print(f"节点 {len(result['nodes'])} 个、连线 {len(workflow.get('links', []))} 条、"
          f"分组 {len(workflow.get('groups', []))} 个")
    used_types = sorted({str(n.get('type', '')) for n in result['nodes']})
    print(f"用到的节点类型 {len(used_types)} 种")

    # ---------------- 2. 缺失节点 ----------------
    section("2. 节点是否齐全")
    if result["missing_types"]:
        for ntype, ids in sorted(result["missing_types"].items()):
            say("错误", f"节点类型未安装：{ntype}", f"出现在节点 id：{ids}")
        print("解决办法：在 ComfyUI-Manager 里安装对应插件，或从作者处获取该自定义节点后重启 ComfyUI。")
    else:
        say("OK", f"工作流用到的 {len(used_types)} 种节点，本机 ComfyUI 全部都有")

    # ---------------- 3. 模型/文件是否齐全 ----------------
    section("3. 引用的模型与素材文件是否齐全")
    if result["missing_values"]:
        for nid, ntype, name, value, total, mode in result["missing_values"]:
            tag = "" if mode == 0 else f"（该节点为“{MODE_NAMES.get(mode, mode)}”，暂不影响运行）"
            say("错误" if mode == 0 else "提示",
                f"节点 {nid} {ntype}.{name} 引用的文件不存在{tag}",
                f"工作流里写的是：{value}\n本机同类可选值共 {total} 个")
        print()
        print("说明：“可选值”就是 ComfyUI 实际扫到的文件列表。")
        print("解决办法：把缺失文件放进对应模型目录，或在界面里改选已有的文件。")
    else:
        say("OK", "工作流引用的模型文件在本机都能找到")

    # ---------------- 4. 素材需求 ----------------
    section("4. 运行需要准备的外部素材")
    if result["active_inputs"]:
        for nid, ntype, title, value in result["active_inputs"]:
            exists = (input_dir / value).exists() if value else False
            say("OK" if exists else "错误", f"节点 {nid} {ntype} “{title}”：{'已有' if exists else '缺文件'}",
                f"文件名：{value or '(未填写)'}\n应放在：{input_dir}")
    else:
        say("提示", "没有处于启用状态的外部素材节点（图片/视频/音频）")

    # ---------------- 5. 可参数化项 ----------------
    section("5. 需要按片段替换的参数")
    for nid, ntype, label, detail in result["params"]:
        print(f"节点 {nid}  {ntype}  —— {label}")
        print(f"        当前值：{detail}")

    # ---------------- 结论 ----------------
    section("结论")
    if hard_errors:
        say("错误", f"发现 {hard_errors} 处会导致无法运行的问题", "请按第 2、3 节的提示补齐后重新体检。")
        return 1
    say("OK", "工作流的节点与引用文件都已就位", "下一步可进行 UI→API 格式转换与试跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
