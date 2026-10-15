"""事实单一来源的回归测试（阶段 8）。

守两件事：
1. 提示词里的**参考图声明行**与**人物锁定行**必须由程序从事实表生成，不许模型自由发挥；
2. 画面描述里点名的角色必须登记进 `characters_in_frame`，否则锁定行会漏人。

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

import make_prompts  # noqa: E402
import make_storyboard  # noqa: E402

FILM = PROJECT_ROOT / "projects" / "animal-heads-nap-01"


class DerivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.board = json.loads((FILM / "storyboard.json").read_text(encoding="utf-8"))
        cls.pack = json.loads((FILM / "prompt_pack.json").read_text(encoding="utf-8"))
        cls.bible = json.loads((FILM / "screenplay.json").read_text(encoding="utf-8"))["continuity_bible"]
        cls.registry = {a["asset_id"]: a for a in cls.board["asset_registry"]}

    def test_declaration_covers_every_submitted_image(self) -> None:
        """声明行必须与提交的参考图一一对应、编号连续——这是程序拼的，不是模型写的。"""
        for shot in self.pack["shots"]:
            with self.subTest(shot=shot["shot_id"]):
                derived = make_prompts.derive_declaration(shot, self.registry)
                self.assertEqual(derived, shot["prompt_lines"]["ref_declaration"])
                for index, img in enumerate(shot["reference_images"]):
                    self.assertIn(f"@图片{index + 1}", derived)
                self.assertNotIn(f"@图片{len(shot['reference_images']) + 1}", derived)
                self.assertIn(f"以上 {len(shot['reference_images'])} 张", derived)

    def test_lock_line_uses_bible_appearance_verbatim(self) -> None:
        """锁定行的外观/服装文字必须逐字来自剧本角色表（只有一处来源，不可能漂移）。"""
        chars = {c["id"]: c for c in self.bible["characters"]}
        board_shots = {s["shot_id"]: s for s in self.board["shots"]}
        for shot in self.pack["shots"]:
            fact = dict(shot)
            fact["characters_in_frame"] = board_shots[shot["shot_id"]]["characters_in_frame"]
            derived = make_prompts.derive_character_lock(fact, self.registry, self.bible)
            self.assertEqual(derived, shot["prompt_lines"]["character_lock"])
            for cid in fact["characters_in_frame"]:
                char = chars[cid]
                self.assertIn(char["appearance"], derived, f"{cid} 的外观没进锁定行")
                self.assertIn(char["wardrobe"], derived, f"{cid} 的服装没进锁定行")

    def test_shot_without_characters_is_declared_as_such(self) -> None:
        shot = copy.deepcopy(self.pack["shots"][0])
        shot["characters_in_frame"] = []
        text = make_prompts.derive_character_lock(shot, self.registry, self.bible)
        self.assertIn("不出现任何角色", text)


class FactConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.board = json.loads((FILM / "storyboard.json").read_text(encoding="utf-8"))
        cls.bible = json.loads((FILM / "screenplay.json").read_text(encoding="utf-8"))["continuity_bible"]

    def test_baseline_is_clean(self) -> None:
        self.assertEqual([], make_storyboard.storyboard_problems(self.board, self.conf, 30.0, self.bible))

    def test_character_named_in_text_must_be_declared(self) -> None:
        board = copy.deepcopy(self.board)
        board["shots"][0]["characters_in_frame"] = ["CH01"]      # 漏掉羚羊/剪影/后脑勺
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0, self.bible)
        self.assertTrue(any("characters_in_frame 没登记" in p for p in problems), problems)

    def test_unknown_character_id_rejected(self) -> None:
        board = copy.deepcopy(self.board)
        board["shots"][0]["characters_in_frame"] = ["CH01", "CH99"]
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0, self.bible)
        self.assertTrue(any("CH99" in p for p in problems), problems)

    def test_asset_character_link_must_exist(self) -> None:
        board = copy.deepcopy(self.board)
        board["asset_registry"][0]["character_id"] = "CH99"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0, self.bible)
        self.assertTrue(any("character_id=CH99" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
