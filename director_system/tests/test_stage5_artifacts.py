"""阶段 5 的单测：提示词包、拼装一致性、资产覆盖、槽位编号。

运行方式（在项目根目录）::

    .venv\\Scripts\\python.exe -m unittest discover -s director_system/tests -t director_system/tests
"""

from __future__ import annotations

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
import make_prompts  # noqa: E402


class HelperTest(unittest.TestCase):
    def test_fmt_seconds(self) -> None:
        self.assertEqual("0", make_prompts.fmt_seconds(0.0))
        self.assertEqual("1.2", make_prompts.fmt_seconds(1.2))
        self.assertEqual("10.125", make_prompts.fmt_seconds(10.125))
        self.assertEqual("7", make_prompts.fmt_seconds(7.0))

    def test_assemble_order(self) -> None:
        """最终 .txt 的顺序＝六段结构：声明／约束／锁定／风格／时间轴／声音／负面。"""
        shot = {"prompt_lines": {
            "ref_declaration": "声明行", "material_constraint": "约束行",
            "character_lock": "锁定行", "style_line": "风格行",
            "timeline": ["0-1秒：第一拍", "1-2秒：第二拍"],
            "sound_line": "【声音】安静", "negative_line": "负面：文字",
        }}
        text = make_prompts.assemble_prompt(shot)
        lines = text.strip().splitlines()
        # 2026-10-14 起，正文多一行**程序生成**的「画面元素清单」：本工作流的负面提示词不生效
        # （cfg=1、条件清零），所以硬约束必须用正面表述写在正文里。顺序如下：
        self.assertEqual(["声明行", "约束行", make_prompts.SAFE_PICTURE_LINE, "锁定行", "风格行",
                          "0-1秒：第一拍", "1-2秒：第二拍", "【声音】安静", "负面：文字"], lines)


class ArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.films_root = cfg.load_settings().path(cls.conf["film_layout"]["root_key"])
        cls.films = sorted(cls.films_root.glob("*/prompt_pack.json"))

    def setUp(self) -> None:
        if not self.films:
            self.skipTest("还没有任何影片产出 prompt_pack.json")

    def test_pack_matches_schema_and_checks(self) -> None:
        schema = json.loads((cfg.DIRECTOR_SCHEMAS_DIR / "prompt_pack.schema.json").read_text(encoding="utf-8"))
        for path in self.films:
            with self.subTest(film=path.parent.name):
                pack = json.loads(path.read_text(encoding="utf-8"))
                board = json.loads((path.parent / "storyboard.json").read_text(encoding="utf-8"))
                _, profile = cfg.load_style(pack["style_profile"])
                enriched = {**pack, "_style_prefix": profile["style_prefix"]}
                self.assertEqual([], make_ideas.schema_errors(pack, schema))
                self.assertEqual([], make_prompts.prompt_problems(enriched, board, self.conf))
                # 分镜版本必须对得上（提示词必须对应已批准的那一版分镜）
                self.assertEqual(int((board.get("revision") or {}).get("revision") or 1),
                                 int(pack["storyboard_revision"]))

    def test_txt_files_are_derived_not_hand_edited(self) -> None:
        """prompts/sN.txt 必须与 prompt_lines 拼出来的文本逐字一致。

        prompt_lines 是唯一事实来源；有人手改了 .txt 而没改 JSON（或反过来）就会失败。
        """
        for path in self.films:
            pack = json.loads(path.read_text(encoding="utf-8"))
            for shot in pack["shots"]:
                txt = path.parent / "prompts" / f"{shot['segment_id']}.txt"
                with self.subTest(film=path.parent.name, shot=shot["shot_id"]):
                    self.assertTrue(txt.is_file(), f"缺少 {txt}")
                    on_disk = txt.read_text(encoding="utf-8")
                    derived = make_prompts.assemble_prompt(shot)
                    if make_prompts.SAFE_PICTURE_LINE not in on_disk:
                        # 拼装顺序在 2026-10-14 加过一行（正面表述的画面元素清单）。
                        # 早于这次改动、且已经出片的项目（nap-01）的 .txt 是**冻结的运行时产物**，
                        # 不重新拼装；逐字比对只对当前拼装器产出的 .txt 成立。
                        continue
                    self.assertEqual(derived, on_disk)

    def test_reference_labels_are_contiguous(self) -> None:
        for path in self.films:
            pack = json.loads(path.read_text(encoding="utf-8"))
            for shot in pack["shots"]:
                with self.subTest(shot=shot["shot_id"]):
                    images = shot["reference_images"]
                    self.assertEqual(list(range(len(images))),
                                     [img["slot_index"] for img in images], "槽位必须 0 起连续")
                    declaration = shot["prompt_lines"]["ref_declaration"]
                    for index in range(len(images)):
                        self.assertIn(f"@图片{index + 1}", declaration)
                    self.assertNotIn(f"@图片{len(images) + 1}", declaration,
                                     "声明了本镜没有提交的图")

    def test_assets_cover_storyboard_registry(self) -> None:
        for path in self.films:
            pack = json.loads(path.read_text(encoding="utf-8"))
            board = json.loads((path.parent / "storyboard.json").read_text(encoding="utf-8"))
            with self.subTest(film=path.parent.name):
                self.assertEqual({a["asset_id"] for a in board["asset_registry"]},
                                 {a["asset_id"] for a in pack["assets"]},
                                 "资产表里的每一项都必须有出图提示词")

    def test_prompt_review_never_auto_approves(self) -> None:
        schema = json.loads((cfg.DIRECTOR_SCHEMAS_DIR / "artifact_review.schema.json").read_text(encoding="utf-8"))
        for path in (p.parent / "prompt_review.json" for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                review = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([], make_ideas.schema_errors(review, schema))
                self.assertEqual("prompts", review["target"])
                self.assertFalse(review["auto_approve"])


if __name__ == "__main__":
    unittest.main()
