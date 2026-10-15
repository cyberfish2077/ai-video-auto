"""给出图工作流的模型显式指定注意力后端（阶段十，2026-10-14）。

背景：用户要求 **固定开启 SageAttention**（启动参数里的 `--use-sage-attention`）。
但实测这台机器上 Qwen-Image 出图在 sage 内核里会原生崩溃
（`comfy/ldm/modules/attention.py:716 attention_sage`，每 1~2 个任务死一次）。
H3 视频工作流自己带 `ModelAttentionBackend` 节点、显式指定了非 sage 的后端，
所以那条路一直是稳的。

本脚本做同样的事：在出图工作流的 KSampler 前面插一个 `ModelAttentionBackend`，
把该模型的注意力后端显式指定为 `comfy kitchen attention`（INT8 量化注意力，
与 H3 工作流同款）。这样全局 sage 保持开启，出图却不再走 sage 内核。

只改**本项目 workflows/ 里的副本**，不动 ComfyUI 目录里的原工作流。

用法::

    .venv\\Scripts\\python.exe scripts\\add_attention_backend.py            # 处理默认三份
    .venv\\Scripts\\python.exe scripts\\add_attention_backend.py --backend "pytorch attention"
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 默认处理这三份出图工作流（视频那条已经有自己的注意力节点，不用动）
DEFAULT_WORKFLOWS = [
    "Qwen-Image-2.1-文生图.json",
    "Qwen-Image-2.1_图像编辑.json",
    "Qwen-Image-2.1_多图参考.json",
]


def patch(path: Path, backend: str) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = {int(n["id"]): n for n in data["nodes"]}
    if any(n.get("type") == "ModelAttentionBackend" for n in data["nodes"]):
        return "已经插过了，跳过"

    sampler = next((n for n in data["nodes"] if n.get("type") == "KSampler"), None)
    if sampler is None:
        return "找不到 KSampler，跳过"
    model_input = next((i for i in (sampler.get("inputs") or []) if i.get("name") == "model"), None)
    if model_input is None or model_input.get("link") is None:
        return "KSampler 的 model 输入没有连线，跳过"

    old_link_id = int(model_input["link"])
    link = next((l for l in data["links"] if int(l[0]) == old_link_id), None)
    if link is None:
        return f"找不到连线 {old_link_id}，跳过"
    source_id = int(link[1])

    new_node_id = max(nodes) + 1
    new_link_id = max(int(l[0]) for l in data["links"]) + 1
    template = nodes[source_id]  # 借它的坐标，好认
    new_node = {
        "id": new_node_id,
        "type": "ModelAttentionBackend",
        "pos": [float(template["pos"][0]) + 320, float(template["pos"][1])],
        "size": [294.53, 86],
        "flags": {},
        "order": int(sampler.get("order", 0)),
        "mode": 0,
        "inputs": [
            {"label": "model", "name": "model", "type": "MODEL", "link": old_link_id},
            {"label": "attention", "name": "attention", "type": "COMBO",
             "widget": {"name": "attention"}, "link": None},
        ],
        "outputs": [{"label": "model", "name": "model", "type": "MODEL", "links": [new_link_id]}],
        "properties": {"cnr_id": "comfy-core", "ver": "0.39.0",
                       "Node name for S&R": "ModelAttentionBackend"},
        "widgets_values": [backend],
    }
    data["nodes"].append(new_node)

    # 原连线改到新节点上，再从新节点接到 KSampler
    link[3] = new_node_id
    link[4] = 0
    model_input["link"] = new_link_id
    data["links"].append([new_link_id, new_node_id, 0, int(sampler["id"]),
                          [i.get("name") for i in sampler["inputs"]].index("model"), "MODEL"])
    data["last_node_id"] = max(data.get("last_node_id", 0), new_node_id)
    data["last_link_id"] = max(data.get("last_link_id", 0), new_link_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="")
    return f"已插入节点 {new_node_id}（后端 {backend}）"


def main() -> int:
    parser = argparse.ArgumentParser(description="给出图工作流指定注意力后端")
    parser.add_argument("--backend", default="comfy kitchen attention",
                        help="ModelAttentionBackend 的取值（默认 comfy kitchen attention）")
    parser.add_argument("--workflow", action="append", help="只处理指定文件（可重复）")
    args = parser.parse_args()

    targets = args.workflow or [str(PROJECT_ROOT / "workflows" / n) for n in DEFAULT_WORKFLOWS]
    for item in targets:
        path = Path(item)
        if not path.exists():
            print(f"[跳过] 找不到 {path}")
            continue
        print(f"{path.name}：{patch(path, args.backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
