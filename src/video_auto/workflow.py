"""UI 格式工作流 → API 格式 prompt 的转换（阶段五）。

ComfyUI 界面保存的工作流（顶层含 ``nodes`` / ``links``）不能直接提交给 ``/prompt``，
必须先转成 ``{"节点id": {"class_type": ..., "inputs": {...}}}`` 这种字典。

转换规则（与 ComfyUI 前端行为一致）：

1. ``mode=4``（已跳过）的节点不提交，其输出按“同类型直通”接到上游输入；
2. ``mode=2``（静音）的节点不提交，下游依赖它的输入视为断线；
3. ``PrimitiveNode``（前端的数字/文本节点）不提交，它的值直接内联到下游；
4. ``Note`` / ``MarkdownNote`` / 界面开关节点等不提交；
5. 控件值按“节点类型定义中的控件顺序”与 ``widgets_values`` 对齐，
   KSampler 之类前端会多插一个 ``control_after_generate``，已单独处理。

一切节点定义都来自 ComfyUI 的 ``/object_info``，不猜测任何参数名。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 只在前端存在、不能提交给后端的节点类型
FRONTEND_ONLY_TYPES = {
    "Note",
    "MarkdownNote",
    "PrimitiveNode",
    "Reroute",
    "RerouteNode",
}

#: 前端自动追加的控件，占一个 widgets_values 位置但不提交
CONTROL_SUFFIX = "\x00control_after_generate"

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN"}


def is_widget_type(head: str) -> bool:
    """判断某个输入类型在界面上是不是“控件”（下拉框/输入框）。

    除普通控件外还要覆盖几种下拉框写法：``COMBO``、``COMBO_DYNAMICCOMBO_V3``、
    ``COMFY_DYNAMICCOMBO_V3``（最后一类的可选值由别的控件动态决定，
    例如图像保存格式，取值不校验但必须原样传给后端）。
    """
    upper = str(head).upper()
    return upper in WIDGET_TYPES or upper.startswith("COMBO") or "DYNAMICCOMBO" in upper


def is_dynamic_combo_type(head: str) -> bool:
    """是不是"动态下拉框"（选中某个值之后会带出联动输入，例如保存格式）。"""
    return "DYNAMICCOMBO" in str(head).upper()


def dynamic_option_inputs(spec: list, selected: Any) -> list[tuple[str, str, list[str]]]:
    """展开动态下拉框在选中某个值之后才出现的联动输入。

    例如 SaveImageAdvanced 的 ``format``：选 ``png`` 会要求 ``bit_depth`` 与
    ``input_color_space``，选 ``exr`` 要求的又是另一组。这些联动输入不在
    ``object_info`` 的 required/optional 里，必须按当前选中的值展开，
    否则提交时会被后端判为"缺少必填输入"。
    """
    if not isinstance(spec, list) or len(spec) < 2 or not isinstance(spec[1], dict):
        return []
    options = spec[1].get("options")
    if not isinstance(options, list) or not options:
        return []
    chosen = next(
        (opt for opt in options if isinstance(opt, dict) and str(opt.get("key")) == str(selected)),
        None,
    )
    if chosen is None:
        chosen = options[0] if isinstance(options[0], dict) else None
    if not chosen:
        return []
    rows: list[tuple[str, str, list[str]]] = []
    for group in ("required", "optional"):
        for name, dep in (chosen.get("inputs", {}).get(group, {}) or {}).items():
            if not isinstance(dep, list) or not dep:
                continue
            dep_options = combo_options(dep)
            if dep_options:
                rows.append((str(name), "enum", dep_options))
            elif isinstance(dep[0], str) and is_widget_type(dep[0]):
                rows.append((str(name), str(dep[0]).lower(), []))
    return rows


class ConvertError(RuntimeError):
    """转换过程中发现会阻止运行的问题。错误信息必须能指导修复。"""


def combo_options(spec: list) -> list[str]:
    """取出输入定义中的可选值列表（兼容 ``[列表, {...}]`` 与 ``["COMBO", {...}]``）。"""
    if not isinstance(spec, list) or not spec:
        return []
    head = spec[0]
    if isinstance(head, list):
        return [str(x) for x in head]
    if head == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return [str(x) for x in spec[1].get("options", [])]
    return []


def spec_input_names(spec_node: dict) -> list[tuple[str, str]]:
    """按声明顺序返回 ``(输入名, 类型)``，required 在前、optional 在后。"""
    rows: list[tuple[str, str]] = []
    for group in ("required", "optional"):
        for name, spec in (spec_node.get("input", {}).get(group, {}) or {}).items():
            head = spec[0] if isinstance(spec, list) and spec else "?"
            if isinstance(head, list):
                head = "COMBO"
            rows.append((str(name), str(head)))
    return rows


def widget_rows(
    spec_node: dict,
    selected: dict[str, Any] | None = None,
) -> list[tuple[str, str, list[str]]]:
    """返回控件型输入 ``(名字, 类别, 可选值)``，顺序与 ``widgets_values`` 对齐。

    ``selected`` 传当前已知的控件取值，用于展开动态下拉框的联动输入。
    """
    rows: list[tuple[str, str, list[str]]] = []
    for group in ("required", "optional"):
        for name, spec in (spec_node.get("input", {}).get(group, {}) or {}).items():
            if not isinstance(spec, list) or not spec:
                continue
            head = spec[0]
            meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            options = combo_options(spec)
            if isinstance(head, list):
                # 包括**空列表**的情况：那代表"本机该目录下没有任何可用文件"，
                # 也必须作为控件把值提交出去，后端才会给出准确的报错。
                rows.append((str(name), "enum", options))
            elif isinstance(head, str) and not meta.get("forceInput") and is_widget_type(head):
                rows.append((str(name), head.lower(), []))
                if is_dynamic_combo_type(head):
                    rows.extend(dynamic_option_inputs(spec, (selected or {}).get(str(name))))
            if meta.get("control_after_generate"):
                rows.append((CONTROL_SUFFIX, "control", []))
    return rows


def map_widgets(spec_node: dict, raw_values: Any) -> dict[str, Any]:
    """把 ``widgets_values`` 映射成 ``{控件名: 值}``。

    分两遍：第一遍按基础控件对齐、拿到当前取值；第二遍用这些取值展开
    动态下拉框的联动输入。这样位置才不会因为联动输入而错开。
    """
    if isinstance(raw_values, dict):
        return {k: v for k, v in raw_values.items() if k != "videopreview"}
    if not isinstance(raw_values, list):
        return {}
    names = [name for name, _, _ in widget_rows(spec_node)]
    selected = {
        name: value for name, value in zip(names, raw_values) if name != CONTROL_SUFFIX
    }
    names = [name for name, _, _ in widget_rows(spec_node, selected=selected)]
    return {
        name: value for name, value in zip(names, raw_values) if name != CONTROL_SUFFIX
    }


def dynamic_combo_groups(
    spec_node: dict,
    selected: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """找出每个“动态下拉框”控件及它当前选中值下的联动输入名。

    返回 ``{控件名: [联动输入名, ...]}``。提交时必须把联动输入打包进这个控件：

    .. code-block:: json

        "format": {"format": "png", "bit_depth": "8-bit", "input_color_space": "sRGB"}

    选择值的键名就是控件自己的名字 —— 这是 ComfyUI 0.39 的 ``IO.DynamicCombo``
    约定，已通过节点源码 ``format["format"]`` 与服务端校验接口双向确认。
    """
    groups: dict[str, list[str]] = {}
    for group in ("required", "optional"):
        for name, spec in (spec_node.get("input", {}).get(group, {}) or {}).items():
            if not isinstance(spec, list) or not spec:
                continue
            head = spec[0]
            if isinstance(head, str) and is_dynamic_combo_type(head):
                groups[str(name)] = [
                    row[0] for row in dynamic_option_inputs(spec, (selected or {}).get(str(name)))
                ]
    return groups


def load_ui_workflow(path: Path, object_info: dict) -> "UiWorkflow":
    if not path.exists():
        raise ConvertError(f"找不到工作流文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConvertError(f"工作流不是合法 JSON：{path}\n{exc}") from exc
    if "nodes" not in data or "links" not in data:
        raise ConvertError(
            f"这看起来不是 UI 格式工作流（缺少 nodes/links）：{path}\n"
            f"说明：本程序需要的是 ComfyUI 界面保存的那份工作流。"
        )
    return UiWorkflow(data, object_info, path)


class UiWorkflow:
    """UI 格式工作流的只读封装，并提供转成 API 格式的能力。"""

    def __init__(self, data: dict, object_info: dict, source: Path) -> None:
        self.data = data
        self.object_info = object_info
        self.source = source
        self.nodes: dict[int, dict] = {int(n["id"]): n for n in data.get("nodes", [])}
        self.links: dict[int, dict] = {}
        self.input_link: dict[tuple[int, int], dict] = {}
        for raw in data.get("links", []):
            link = {
                "id": int(raw[0]),
                "src_id": int(raw[1]),
                "src_slot": int(raw[2]),
                "dst_id": int(raw[3]),
                "dst_slot": int(raw[4]),
                "type": raw[5] if len(raw) > 5 else None,
            }
            self.links[link["id"]] = link
            self.input_link[(link["dst_id"], link["dst_slot"])] = link

    # ---------------- 基础访问 ----------------

    def type_of(self, node_id: int) -> str:
        return str(self.nodes.get(node_id, {}).get("type", ""))

    def mode_of(self, node_id: int) -> int:
        return int(self.nodes.get(node_id, {}).get("mode", 0) or 0)

    def widget_map(self, node: dict) -> dict[str, Any]:
        """把 ``widgets_values`` 映射成 ``{控件名: 值}``。"""
        ntype = str(node.get("type", ""))
        if ntype == "PrimitiveNode":
            raw = node.get("widgets_values")
            if isinstance(raw, list) and raw:
                return {"value": raw[0]}
            return {}
        spec_node = self.object_info.get(ntype)
        if spec_node is None:
            return {}
        raw = node.get("widgets_values")
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if k != "videopreview"}
        if not isinstance(raw, list):
            return {}
        return map_widgets(spec_node, raw)

    # ---------------- 连线解析 ----------------

    def _resolve_output(self, node_id: int, slot: int, seen: frozenset[int] = frozenset()):
        """解析某节点某个输出槽最终的数据来源。

        返回 ``("link", 节点id, 槽号)``、``("value", 字面值)`` 或 ``None``（断线）。
        """
        if node_id in seen:
            raise ConvertError(f"检测到循环连线，涉及节点 {node_id}")
        node = self.nodes.get(node_id)
        if node is None:
            return None
        ntype = str(node.get("type", ""))
        mode = int(node.get("mode", 0) or 0)

        if ntype == "PrimitiveNode":
            values = self.widget_map(node)
            return ("value", values.get("value")) if values else None

        if ntype in ("Reroute", "RerouteNode"):
            return self._source_of_input(node, 0, seen | {node_id})

        if mode == 4:  # 已跳过：同类型直通
            return self._passthrough(node, slot, seen | {node_id})

        if mode == 2:  # 静音：不产生数据
            return None

        if ntype in FRONTEND_ONLY_TYPES or ntype not in self.object_info:
            return None

        return ("link", node_id, slot)

    def _passthrough(self, node: dict, out_slot: int, seen: frozenset[int]):
        outputs = node.get("outputs") or []
        out_type = outputs[out_slot].get("type") if out_slot < len(outputs) else None
        for index, inp in enumerate(node.get("inputs") or []):
            in_type = inp.get("type")
            # "*" 是通配类型（例如 ComfyNotNode 的输入），可以匹配任意输出
            if out_type and out_type != "*" and in_type not in (None, "*", out_type):
                continue
            source = self._source_of_input(node, index, seen)
            if source is not None:
                return source
        return None

    def _source_of_input(self, node: dict, index: int, seen: frozenset[int]):
        inputs = node.get("inputs") or []
        if index >= len(inputs):
            return None
        inp = inputs[index]
        link_id = inp.get("link")
        if link_id is not None and int(link_id) in self.links:
            link = self.links[int(link_id)]
            return self._resolve_output(link["src_id"], link["src_slot"], seen)
        name = str(inp.get("name", ""))
        values = self.widget_map(node)
        if name in values and values[name] is not None:
            return ("value", values[name])
        return None

    def input_sources(self, node: dict) -> dict[str, Any]:
        """返回该节点所有**已连线**输入的实际内容：``{输入名: [上游id, 槽号] 或 字面值}``。"""
        result: dict[str, Any] = {}
        for index, inp in enumerate(node.get("inputs") or []):
            name = str(inp.get("name", ""))
            if inp.get("link") is None:
                continue
            source = self._source_of_input(node, index, frozenset({int(node["id"])}))
            if source is None:
                continue
            if source[0] == "link":
                result[name] = [str(source[1]), source[2]]
            else:
                result[name] = source[1]
        return result

    # ---------------- 体检与转换 ----------------

    def participates(self, node: dict) -> bool:
        """该节点是否真的参与计算图（有输入连线，或它的输出被别的节点引用）。"""
        node_id = int(node["id"])
        for inp in node.get("inputs") or []:
            if inp.get("link") is not None:
                return True
        return any(link["src_id"] == node_id for link in self.links.values())

    def missing_node_types(self) -> dict[str, list[int]]:
        """会阻止运行的缺失节点：本机没有该类型，**且**它在计算图里被连线使用。

        像 rgthree 的界面开关这类"悬空"节点不参与计算，忽略即可，不算缺失。
        """
        missing: dict[str, list[int]] = {}
        for node_id, node in self.nodes.items():
            ntype = str(node.get("type", ""))
            if ntype in FRONTEND_ONLY_TYPES or ntype in self.object_info:
                continue
            if not self.participates(node):
                continue
            missing.setdefault(ntype, []).append(node_id)
        return missing

    def ignored_node_types(self) -> dict[str, list[int]]:
        """被忽略的节点：本机没有该类型，但也没连线，不影响运行。"""
        ignored: dict[str, list[int]] = {}
        for node_id, node in self.nodes.items():
            ntype = str(node.get("type", ""))
            if ntype in FRONTEND_ONLY_TYPES or ntype in self.object_info:
                continue
            if self.participates(node):
                continue
            ignored.setdefault(ntype, []).append(node_id)
        return ignored

    def to_api_prompt(
        self,
        overrides: dict[int, dict[str, Any]] | None = None,
        bypass_nodes: set[int] | None = None,
    ) -> dict[str, dict]:
        """转换成 API 格式的 prompt 字典。

        ``overrides`` 形如 ``{节点id: {控件名: 新值}}``，用于替换提示词、时长、种子等。
        ``bypass_nodes`` 里的节点按 ComfyUI 的"跳过"语义处理：不提交，
        且下游引用它会拿不到数据 —— 用于"参考图槽位按需加载"（没给图的槽位跳过）。
        """
        overrides = overrides or {}
        bypass_nodes = bypass_nodes or set()
        missing = self.missing_node_types()
        if missing:
            detail = "\n".join(f"  - {t}：节点 {ids}" for t, ids in sorted(missing.items()))
            raise ConvertError(
                "工作流里有本机未安装的节点，无法提交：\n" + detail +
                "\n解决办法：安装对应插件后重启 ComfyUI，或改用不依赖这些节点的工作流。"
            )

        # 把要跳过的节点临时标记为 mode=4（与 ComfyUI 界面上的"跳过"一致），
        # 这样连线解析会走直通逻辑，下游拿不到它的输出。
        restore: dict[int, int] = {}
        for node_id in bypass_nodes:
            node = self.nodes.get(node_id)
            if node is not None:
                restore[node_id] = int(node.get("mode", 0) or 0)
                node["mode"] = 4
        try:
            return self._build_prompt(overrides)
        finally:
            for node_id, mode in restore.items():
                self.nodes[node_id]["mode"] = mode

    def _build_prompt(self, overrides: dict[int, dict[str, Any]]) -> dict[str, dict]:
        """实际拼装 API prompt 的内部实现（调用前已处理好跳过节点）。"""
        prompt: dict[str, dict] = {}
        problems: list[str] = []
        for node_id in sorted(self.nodes):
            node = self.nodes[node_id]
            ntype = str(node.get("type", ""))
            mode = int(node.get("mode", 0) or 0)
            if ntype in FRONTEND_ONLY_TYPES or mode != 0:
                continue
            spec_node = self.object_info.get(ntype)
            if spec_node is None:
                continue

            values = self.widget_map(node)
            explicit = overrides.get(node_id) or {}
            for key in explicit:
                if key not in values:
                    raise ConvertError(
                        f"节点 {node_id}（{ntype}）没有名为 {key} 的控件，无法改写。\n"
                        f"该节点可改写的控件：{sorted(values)}\n"
                        f"解决办法：查看 config/workflow_targets.yaml 的 overrides 配置，"
                        f"或先跑 scripts/inspect_workflow.py 核对节点 id。"
                    )

            linked = self.input_sources(node)
            inputs: dict[str, Any] = {}
            spec_names = dict(spec_input_names(spec_node))

            for name, value in linked.items():
                base = name.split(".", 1)[0]
                if name in spec_names or base in spec_names:
                    inputs[name] = value

            for name, kind, _options in widget_rows(spec_node, selected=values):
                if name in linked or name in inputs:
                    continue
                if name not in values or values[name] is None:
                    continue
                inputs[name] = values[name]

            # 显式改写优先级最高：即使该输入被连线占用，也按用户指定的值提交。
            # 这一步是为了让"双保险"成立（例如把开关的两个分支都设成同一个提示词）。
            for key, value in explicit.items():
                values[key] = value
                inputs[key] = value

            # 动态下拉框：把选中值与其联动输入打包成嵌套字典再提交
            for combo_id, dep_names in dynamic_combo_groups(spec_node, values).items():
                if combo_id not in inputs:
                    continue
                nested: dict[str, Any] = {combo_id: inputs.pop(combo_id)}
                for dep in dep_names:
                    if dep in inputs:
                        nested[dep] = inputs.pop(dep)
                inputs[combo_id] = nested

            required = spec_node.get("input", {}).get("required") or {}
            optional = spec_node.get("input", {}).get("optional") or {}
            for name, kind in spec_input_names(spec_node):
                if name in inputs or name in optional:
                    continue
                if kind in ("?", "COMBO") or "AUTOGROW" in str(kind):
                    continue
                problems.append(f"节点 {node_id}（{ntype}）缺少必填输入 {name}")

            prompt[str(node_id)] = {"class_type": ntype, "inputs": inputs}

        if problems:
            raise ConvertError(
                "转换后发现必填输入缺失，已阻止提交：\n"
                + "\n".join(f"  - {p}" for p in problems)
            )
        if not prompt:
            raise ConvertError("转换结果为空，请检查工作流是否所有节点都被跳过。")
        return prompt
