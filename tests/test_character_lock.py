"""【人物锁定】提示词补丁的单元测试（纯逻辑，不联网、不占显卡）。

背景：2026-10-13 实测——提示词里只写"两人"、且不写服装颜色时，模型会把主角画成
随便两个人、并把女主的外套从米白漂成橄榄绿（seg-07/seg-08）。本文件守住两件事：

1. 每段提示词都必须自带【人物锁定】行（谁出场、长什么样、颜色不许改）；
2. 写剧本的 system prompt 必须要求"逐镜点名角色"，不许只写"两人"。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_auto import script_gen  # noqa: E402


def make_story(characters: list[tuple[str, str]]) -> script_gen.Story:
    return script_gen.Story(
        title="测试",
        logline="测试",
        characters=[script_gen.Character(name=n, appearance=a, sheet_prompt="s")
                    for n, a in characters],
        segments=[script_gen.SegmentPlan(
            summary="s", scene_anchor="深夜市区街道", style="紧张",
            shots=[script_gen.Shot(0, 15, "中景，某人站着不说话")],
            sound="风声", storyboard_prompt="p")],
    )


class CharacterLockLineTest(unittest.TestCase):
    def test_lists_every_character_with_image_index_and_appearance(self) -> None:
        story = make_story([
            ("陈默", "男，深灰色连帽卫衣、背黑色双肩包"),
            ("林雨", "女，米白色羽绒外套、低束马尾"),
        ])
        line = script_gen.character_lock_line(story)
        self.assertIn("@图片1“陈默”", line)
        self.assertIn("@图片2“林雨”", line)
        self.assertIn("深灰色连帽卫衣、背黑色双肩包", line)
        self.assertIn("米白色羽绒外套、低束马尾", line)
        self.assertIn("不得替换成陌生面孔", line)
        self.assertIn("颜色不得改变", line)
        self.assertIn("服装与配饰也不要互换", line)

    def test_single_character_has_no_swap_warning(self) -> None:
        line = script_gen.character_lock_line(make_story([("老周头", "男，灰棉袄")]))
        self.assertIn("只出现下列 1 名指定人物", line)
        self.assertNotIn("互换", line)

    def test_appearance_trailing_period_not_duplicated(self) -> None:
        line = script_gen.character_lock_line(make_story([("甲", "深灰卫衣。")]))
        self.assertIn("（深灰卫衣）", line)
        self.assertNotIn("。。", line)


class PromptContainsLockTest(unittest.TestCase):
    def test_every_segment_prompt_carries_the_lock(self) -> None:
        story = make_story([("陈默", "深灰色连帽卫衣"), ("林雨", "米白色羽绒外套")])
        for index in range(len(story.segments)):
            prompt = script_gen.assemble_segment_prompt(
                story, index, segment_seconds=15, aspect="16:9", style_prefix="风格，")
            self.assertIn("【人物锁定】", prompt)
            self.assertIn("米白色羽绒外套", prompt)
            # 锁行必须排在时间轴之前，模型先读到约束再读到动作
            self.assertLess(prompt.index("【人物锁定】"), prompt.index("0-15秒"))

    def test_lock_survives_custom_style_profile(self) -> None:
        story = make_story([("甲", "藏青工装"), ("乙", "红色风衣")])
        prompt = script_gen.assemble_segment_prompt(
            story, 0, segment_seconds=15, aspect="16:9", style_prefix="写实，",
            bgm_line="统一背景音乐：低频轰鸣", negative="卡通")
        self.assertIn("【人物锁定】", prompt)
        self.assertIn("红色风衣", prompt)


class StoryPromptRuleTest(unittest.TestCase):
    def test_system_prompt_forbids_vague_two_people(self) -> None:
        system = script_gen.build_story_messages(
            "讲稿", total_seconds=120, segment_seconds=15, aspect="16:9",
            character_count=2)[0]["content"]
        self.assertIn("逐镜点名角色", system)
        self.assertIn("禁止", system)
        self.assertIn("两人", system)          # 明确把"两人"列为禁用写法
        self.assertIn("storyboard_prompt", system)
        # 规则编号不许串号
        self.assertIn("11. 只输出 JSON", system)
        for n in range(1, 12):
            self.assertIn(f"\n{n}. ", system, f"缺少第 {n} 条硬性要求")


if __name__ == "__main__":
    unittest.main()
