"""风格档位（config/styles.yaml）加载与兼容性单元测试（不联网、不占显卡）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_auto import config as cfg  # noqa: E402
from video_auto import script_gen  # noqa: E402


class LoadStyleTest(unittest.TestCase):
    def test_default_profile_is_guofeng(self) -> None:
        name, profile = cfg.load_style(None)
        self.assertEqual(name, "guofeng-cg")
        self.assertTrue(profile["style_prefix"])
        self.assertTrue(profile["bgm_line"])
        self.assertTrue(profile["negative"])

    def test_explicit_profile(self) -> None:
        name, profile = cfg.load_style("photoreal-cinematic")
        self.assertEqual(name, "photoreal-cinematic")
        for key in ("style_prefix", "bgm_line", "negative", "story_style", "material_style"):
            self.assertIn(key, profile)
            self.assertTrue(profile[key], f"{key} 不应为空")
        self.assertNotIn("国漫", profile["style_prefix"])

    def test_unknown_profile_lists_choices(self) -> None:
        with self.assertRaises(cfg.ConfigError) as ctx:
            cfg.load_style("no-such-style")
        message = str(ctx.exception)
        self.assertIn("no-such-style", message)
        self.assertIn("guofeng-cg", message)   # 报错里必须给出可选档位
        self.assertIn("styles.yaml", message)


class BackwardCompatibilityTest(unittest.TestCase):
    """默认档位必须与历史成片里用的文字逐字一致，否则新旧片风格会不统一。"""

    def test_default_profile_matches_module_constants(self) -> None:
        _, profile = cfg.load_style("guofeng-cg")
        self.assertEqual(profile["bgm_line"], script_gen.BGM_RULE_LINE)
        self.assertEqual(profile["negative"], script_gen.DEFAULT_NEGATIVE)

    def test_prompt_without_overrides_uses_constants(self) -> None:
        story = script_gen.Story(
            title="t", logline="l",
            characters=[script_gen.Character(name="甲", appearance="a", sheet_prompt="s")],
            segments=[script_gen.SegmentPlan(
                summary="s", scene_anchor="场景", style="氛围",
                shots=[script_gen.Shot(0, 15, "中景，某人站着不说话")],
                sound="风声", storyboard_prompt="p")],
        )
        prompt = script_gen.assemble_segment_prompt(
            story, 0, segment_seconds=15, aspect="16:9", style_prefix="风格，")
        self.assertIn(script_gen.BGM_RULE_LINE, prompt)
        self.assertIn(script_gen.DEFAULT_NEGATIVE, prompt)

        overridden = script_gen.assemble_segment_prompt(
            story, 0, segment_seconds=15, aspect="16:9", style_prefix="风格，",
            bgm_line="统一背景音乐：低频轰鸣", negative="卡通、动漫")
        self.assertIn("低频轰鸣", overridden)
        self.assertIn("负面：卡通、动漫。", overridden)
        self.assertNotIn(script_gen.DEFAULT_NEGATIVE, overridden)


class StoryStyleHintTest(unittest.TestCase):
    def test_hint_only_appears_when_given(self) -> None:
        base = script_gen.build_story_messages(
            "讲稿", total_seconds=120, segment_seconds=15, aspect="16:9", character_count=2)
        with_hint = script_gen.build_story_messages(
            "讲稿", total_seconds=120, segment_seconds=15, aspect="16:9", character_count=2,
            style_hint="写实科幻灾难片")
        self.assertNotIn("写实科幻灾难片", base[0]["content"])
        self.assertIn("写实科幻灾难片", with_hint[0]["content"])
        # 除风格块外，其余内容必须完全一致（保证历史行为不变）
        stripped = with_hint[0]["content"].replace(
            "\n画面风格（必须贯彻到每一个镜头，并**逐字体现到 sheet_prompt 与 "
            "storyboard_prompt 的文字描述里**）：写实科幻灾难片\n", "")
        self.assertEqual(base[0]["content"], stripped)


if __name__ == "__main__":
    unittest.main()
