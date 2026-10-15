"""分段与节奏规则的回归测试（用户 2026-10-14 明确的原则）。

守的是首版流程的根因：**不许在创意阶段锁分段与帧数**，也不许为了对齐总时长平均分段。

运行方式（在项目根目录）::

    .venv\\Scripts\\python.exe -m unittest discover -s director_system/tests -t director_system/tests
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(DIRECTOR_ROOT / "scripts"))

from video_auto import config as cfg  # noqa: E402

import make_ideas  # noqa: E402
import make_story  # noqa: E402
import make_storyboard  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_policy_is_recorded_and_complete(self) -> None:
        conf = cfg.load_director_config()
        policy = conf["pacing_policy"]
        self.assertGreaterEqual(len(policy["order"]), 3)
        self.assertIn("镜头边界", policy["order"][0], "第一步必须是定镜头边界")
        self.assertTrue(any("映射" in item for item in policy["order"]),
                        "决策顺序里必须包含「最后映射到合法帧数」这一步")
        self.assertIn("故事", policy["priority"])
        self.assertTrue(any("平均分段" in rule for rule in policy["mapping_rules"]),
                        "映射规则里必须写明禁止为了对齐总时长而平均分段")
        self.assertIn("均匀节奏", policy["equal_length_rule"])


class IdeasStageTest(unittest.TestCase):
    """创意阶段：只写节奏意图，不锁分段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.brief = json.loads(
            (PROJECT_ROOT / "projects" / "animal-heads-nap-01" / "brief.json").read_text(encoding="utf-8"))

    def test_prompt_no_longer_fixes_segments(self) -> None:
        prompt = make_ideas.ideas_user_prompt(
            self.brief, self.conf, make_ideas.load_schema("idea_proposal.schema.json"))
        # 首版就是这段"合法帧数表 + 两个总和选项"把可选空间收窄的，必须不再出现
        self.assertNotIn("29.67", prompt)
        self.assertNotIn("30.38", prompt)
        self.assertNotIn("segment_plan", prompt)
        self.assertIn("pacing_intent", prompt)
        self.assertIn("不要在这个阶段分配分段", prompt)

    def test_schema_requires_pacing_intent_not_segments(self) -> None:
        item = make_ideas.load_schema("idea_proposal.schema.json")["properties"]["concepts"]["items"]
        self.assertIn("pacing_intent", item["required"])
        self.assertNotIn("segment_plan", item["required"])

    def test_locked_concept_has_no_segment_plan(self) -> None:
        ideas = json.loads(
            (PROJECT_ROOT / "projects" / "animal-heads-nap-01" / "ideas.json").read_text(encoding="utf-8"))
        concept = ideas["concepts"][1]
        blob = json.dumps(make_storyboard.locked_concept(concept), ensure_ascii=False)
        self.assertNotIn("segment_plan", blob)
        self.assertIn("pacing_intent", blob)


class StoryStageTest(unittest.TestCase):
    """剧本阶段：时长按叙事意图分配，等长必须给理由。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()

    def test_equal_scenes_need_justification(self) -> None:
        scenes = [{"estimated_seconds": 10.0} for _ in range(3)]
        without = {"total_seconds": 30.0, "scenes": scenes}
        problems = make_story.duration_problems(without, 30.0, self.conf)
        self.assertTrue(any("完全相同" in p for p in problems), problems)

        with_reason = dict(without)
        with_reason["equal_length_justification"] = "这个故事确实需要均匀的三拍节奏：同一动作重复三次。"
        self.assertEqual([], make_story.duration_problems(with_reason, 30.0, self.conf))

    def test_uneven_scenes_pass(self) -> None:
        art = {"total_seconds": 30.0, "scenes": [{"estimated_seconds": 6.0},
                                                  {"estimated_seconds": 16.0},
                                                  {"estimated_seconds": 8.0}]}
        self.assertEqual([], make_story.duration_problems(art, 30.0, self.conf))

    def test_prompt_tells_them_to_plan_by_narrative(self) -> None:
        brief = {"from_user": {"duration_seconds": 30}}
        text = make_story.story_constraints(self.conf, brief)
        self.assertIn("先根据剧本、戏剧节拍", text)
        self.assertIn("不要为了凑总时长把场次切成均等的几块", text)
        self.assertNotIn("17k+5", text, "创意/剧本阶段不该出现帧数表")


class StoryboardStageTest(unittest.TestCase):
    """分镜阶段：理想时长 → 合法帧数的映射规则。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.board = json.loads(
            (PROJECT_ROOT / "projects" / "animal-heads-nap-01" / "storyboard.json").read_text(encoding="utf-8"))

    def test_constraints_carry_legal_table_and_mapping_rules(self) -> None:
        text = make_storyboard.storyboard_constraints(self.conf, 30.0)
        self.assertIn("合法帧数表", text)
        self.assertIn("技术可能性", text)
        self.assertIn("禁止", text)
        self.assertIn("equal_length_justification", text)

    def test_long_ideal_needs_split_explanation(self) -> None:
        board = copy.deepcopy(self.board)
        board["shots"][0]["ideal_edit_duration_sec"] = 20.0
        board["shots"][0]["boundary_reason"] = "就在这里切"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("超过单段上限" in p for p in problems), problems)

        board["shots"][0]["boundary_reason"] = "技术拆分：理想时长超过上限，拆成两个有真实剪辑点的镜头"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertFalse(any("超过单段上限" in p for p in problems), problems)

    def test_short_ideal_needs_handling(self) -> None:
        board = copy.deepcopy(self.board)
        board["shots"][0]["ideal_edit_duration_sec"] = 2.5
        board["shots"][0]["boundary_reason"] = "快切"
        board["shots"][0]["duration_basis"] = "短促"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("低于单段下限" in p for p in problems), problems)

        board["shots"][0]["boundary_reason"] = "与相邻镜头合并为一段连续镜头"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertFalse(any("低于单段下限" in p for p in problems), problems)

    def test_duplicate_segment_ids_rejected(self) -> None:
        board = copy.deepcopy(self.board)
        board["shots"][1]["generation_segment_id"] = board["shots"][0]["generation_segment_id"]
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("重复" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
