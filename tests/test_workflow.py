"""工作流转换的单元测试（阶段五/六）。

重点覆盖容易出错、且出错会"静默跑偏"的地方：

* 动态下拉框（``IO.DynamicCombo``）必须以嵌套字典提交；
* ``mode=4``（跳过）节点按"同类型直通"重新接线；
* 通配符类型 ``*`` 的直通匹配（ComfyNotNode 这类节点）。

运行::

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import workflow as wf  # noqa: E402


def make_object_info() -> dict:
    """构造一份最小但真实的节点定义：含一个动态下拉框节点。"""
    return {
        "SaveImageAdvanced": {
            "input": {
                "required": {
                    "filename_prefix": ["STRING", {"default": "ComfyUI"}],
                    "format": ["COMFY_DYNAMICCOMBO_V3", {
                        "options": [
                            {"key": "png", "inputs": {"required": {
                                "bit_depth": ["COMBO", {"options": ["8-bit", "16-bit"],
                                                        "default": "8-bit"}],
                                "input_color_space": ["COMBO", {"options": ["sRGB"],
                                                                "default": "sRGB"}],
                            }}},
                            {"key": "exr", "inputs": {"required": {
                                "bit_depth": ["COMBO", {"options": ["32-bit float",
                                                                    "16-bit float"],
                                                        "default": "16-bit float"}],
                                "input_color_space": ["COMBO", {"options": ["sRGB", "HDR"],
                                                                "default": "sRGB"}],
                            }}},
                        ]
                    }],
                }
            }
        }
    }


class DynamicComboTest(unittest.TestCase):
    def test_dynamic_combo_is_submitted_as_nested_dict(self) -> None:
        workflow = {
            "nodes": [{"id": 1, "type": "SaveImageAdvanced", "mode": 0,
                       "widgets_values": ["video_auto/test", "png", "8-bit", "sRGB"]}],
            "links": [],
        }
        ui = wf.UiWorkflow(workflow, make_object_info(), Path("stub.json"))
        prompt = ui.to_api_prompt()
        self.assertEqual(prompt["1"]["inputs"]["format"], {
            "format": "png", "bit_depth": "8-bit", "input_color_space": "sRGB",
        })
        # 联动输入不能再以顶层键出现，否则服务端校验会报缺失
        self.assertNotIn("bit_depth", prompt["1"]["inputs"])
        self.assertNotIn("input_color_space", prompt["1"]["inputs"])

    def test_dynamic_combo_follows_selected_option(self) -> None:
        workflow = {
            "nodes": [{"id": 1, "type": "SaveImageAdvanced", "mode": 0,
                       "widgets_values": ["p", "exr", "16-bit float", "HDR"]}],
            "links": [],
        }
        ui = wf.UiWorkflow(workflow, make_object_info(), Path("stub.json"))
        prompt = ui.to_api_prompt()
        self.assertEqual(prompt["1"]["inputs"]["format"], {
            "format": "exr", "bit_depth": "16-bit float", "input_color_space": "HDR",
        })


class BypassSemanticsTest(unittest.TestCase):
    """验证 mode=4 直通与通配符类型匹配。"""

    def test_wildcard_input_matches_any_output(self) -> None:
        object_info = {
            "PrimitiveBoolean": {"input": {"required": {"value": ["BOOLEAN", {}]}}},
            "ComfyNotNode": {"input": {"required": {"value": ["*", {}]}},
                             "output": ["BOOLEAN"]},
            "Switch": {"input": {"required": {"boolean": ["BOOLEAN", {}],
                                              "on_true": ["STRING", {}],
                                              "on_false": ["STRING", {}]}}},
            "Text": {"input": {"required": {"value": ["STRING", {}]}}},
        }
        workflow = {
            "nodes": [
                {"id": 1, "type": "PrimitiveBoolean", "mode": 0, "widgets_values": [False]},
                {"id": 2, "type": "ComfyNotNode", "mode": 4,
                 "inputs": [{"name": "value", "type": "*", "link": 10}],
                 "outputs": [{"name": "BOOLEAN", "type": "BOOLEAN", "links": [11]}]},
                {"id": 3, "type": "Switch", "mode": 0,
                 "inputs": [{"name": "boolean", "type": "BOOLEAN", "link": 11},
                            {"name": "on_true", "type": "STRING", "link": None},
                            {"name": "on_false", "type": "STRING", "link": None}],
                 "widgets_values": ["", "", True]},
            ],
            "links": [[10, 1, 0, 2, 0, "BOOLEAN"], [11, 2, 0, 3, 0, "BOOLEAN"]],
        }
        ui = wf.UiWorkflow(workflow, object_info, Path("stub.json"))
        prompt = ui.to_api_prompt()
        # 跳过的取反节点应被直通：switch 的 boolean 直接指向节点 1
        self.assertEqual(prompt["3"]["inputs"]["boolean"], ["1", 0])
        self.assertNotIn("2", prompt, "跳过的节点不应被提交")


if __name__ == "__main__":
    unittest.main(verbosity=2)
