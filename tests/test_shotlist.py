"""外部镜头清单导入（video_auto.shotlist）的单元测试。

重点守住三条"错了会毁片"的规则：
1. 提示词里 ``@图片N`` 的编号必须与台账里 images 的顺序一一对应；
2. 有角色的镜头，分镜图必须走多图参考台账（否则会被画出路人脸）；
3. 参考图超过工作流槽位时要明确报错，而不是静默丢图。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import shotlist as sl  # noqa: E402


ASSETS_YAML = """
material_style: "写实风格。"
mirror_rule: "两张脸是同一个人。"
characters:
  CHAR_A:
    name_zh: 林泽
    image: char-a.png
    appearance: "中国男性，31 岁，深色外套"
    sheet_prompt: "adult sheet"
  CHAR_B:
    name_zh: 镜中林泽
    image: char-b.png
    appearance: "同一个人的年长版本"
    derive_from: CHAR_A
    edit_prompt: "age him"
environments:
  ENV_ROOM:
    name_zh: 病房
    image: env-room.png
    prompt: "hospital room"
voices:
  "林泽（旁白）":
    instruct: "低沉的男声旁白"
    seed: 7101
  "音效":
    instruct: ""
    seed: 0
"""

SHOTLIST = {
    "project": "试片",
    "version": "1.0",
    "target_total_seconds": 16,
    "shot_count": 2,
    "shots": [
        {
            "shot_id": "01",
            "duration_seconds": 8,
            "title": "空镜",
            "visual_action_zh": "雨夜的公路",
            "dialogue_or_narration_zh": "我叫林泽。",
            "h3_prompt_en": "rainy highway",
            "soundscape_guidance_en": "rain",
            "music_guidance_en": "drone",
            "continuity_guidance_en": "keep the look",
            "reference_asset_keys": ["ENV_ROOM"],
        },
        {
            "shot_id": "02",
            "duration_seconds": 8,
            "title": "镜中",
            "visual_action_zh": "镜子里出现年长的自己",
            "dialogue_or_narration_zh": "另一个我。",
            "h3_prompt_en": "older self in the mirror",
            "soundscape_guidance_en": "hum",
            "music_guidance_en": "drone",
            "continuity_guidance_en": "keep the look",
            "reference_asset_keys": ["ENV_ROOM", "CHAR_B", "CHAR_A"],
        },
    ],
}

SCRIPT_TXT = """《试片》

[01] 8秒 | 空镜
Visual action: 雨夜的公路
Dialogue / narration (林泽（旁白）): 我叫林泽。

[02] 8秒 | 镜中
Visual action: 镜子里出现年长的自己
Dialogue / narration (音效): 低频嗡鸣
"""


class ShotlistTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.assets_path = self.root / "assets.yaml"
        self.assets_path.write_text(ASSETS_YAML, encoding="utf-8")
        self.shotlist_path = self.root / "shotlist.json"
        self.shotlist_path.write_text(json.dumps(SHOTLIST, ensure_ascii=False), encoding="utf-8")
        self.script_path = self.root / "script.txt"
        self.script_path.write_text(SCRIPT_TXT, encoding="utf-8")
        self.assets = sl.load_assets(self.assets_path)
        _meta, self.shots = sl.load_shotlist(self.shotlist_path)
        speakers, lines = sl.load_script_speakers(self.script_path)
        sl.attach_script_speakers(self.shots, speakers, lines)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestAssetsAndShots(ShotlistTestBase):
    def test_loads_characters_and_environments(self) -> None:
        self.assertEqual(set(self.assets["characters"]), {"CHAR_A", "CHAR_B"})
        self.assertEqual(self.assets["environments"]["ENV_ROOM"].image, "env-room.png")
        self.assertEqual(len(self.shots), 2)
        self.assertEqual(self.shots[0].duration, 8.0)

    def test_unknown_reference_key_reports_clearly(self) -> None:
        self.shots[0].refs = ["ENV_NOT_THERE"]
        with self.assertRaises(sl.ShotlistError) as ctx:
            sl.shot_assets(self.shots[0], self.assets["characters"], self.assets["environments"])
        self.assertIn("ENV_NOT_THERE", str(ctx.exception))

    def test_speakers_come_from_script_txt(self) -> None:
        speakers, lines = sl.load_script_speakers(self.script_path)
        self.assertEqual(speakers["01"], "林泽（旁白）")
        self.assertEqual(speakers["02"], "音效")
        self.assertEqual(lines["01"], "我叫林泽。")


class TestReferenceOrder(ShotlistTestBase):
    def test_images_are_characters_then_envs_then_board(self) -> None:
        images, decls, used = sl.shot_assets(
            self.shots[1], self.assets["characters"], self.assets["environments"])
        self.assertEqual(images, ["char-b.png", "char-a.png", "env-room.png", "board-02.png"])
        self.assertEqual(used, ["CHAR_B", "CHAR_A"])
        # @图片N 必须按同一顺序编号
        self.assertIn("@图片1角色“镜中林泽”", decls[0])
        self.assertIn("@图片3image“env-room.png”", decls[2])
        self.assertIn("@图片4image“board-02.png”", decls[3])

    def test_lock_line_points_at_right_slot(self) -> None:
        images, _decls, used = sl.shot_assets(
            self.shots[1], self.assets["characters"], self.assets["environments"])
        lock = sl.character_lock_line(self.assets["characters"], used, images)
        self.assertIn("@图片2“林泽”", lock)
        self.assertIn("不得替换成陌生面孔", lock)

    def test_prompt_carries_bans_and_sound(self) -> None:
        shot = self.shots[1]
        images, decls, used = sl.shot_assets(
            shot, self.assets["characters"], self.assets["environments"])
        prompt = sl.assemble_prompt(
            shot, characters=self.assets["characters"], environments=self.assets["environments"],
            images=images, decls=decls, used_characters=used, style_prefix="写实，",
            aspect="16:9", negative="字幕、水印", mirror_rule=self.assets["mirror_rule"])
        for expected in ("不要出现任何对话或旁白的字幕文字", "【人物锁定】", "【镜像约束】",
                         "【声音】hum", "【音乐】drone", "负面：字幕、水印。", "0-8秒："):
            self.assertIn(expected, prompt)


class TestProductionSplit(ShotlistTestBase):
    def _write(self) -> dict:
        return sl.write_production(
            self.root / "proj", name="试片", meta={"project": "试片", "version": "1.0",
                                                  "shot_count": 2, "target_total_seconds": 16},
            shots=self.shots, assets=self.assets, style_prefix="写实，", aspect="16:9",
            negative="字幕", material_style="写实风格")

    def test_character_shot_goes_to_multiref_boards(self) -> None:
        self._write()
        boards = json.loads((self.root / "proj" / "boards.json").read_text(encoding="utf-8"))
        self.assertEqual(boards["target"], "qwen_multiref")
        self.assertEqual([s["id"] for s in boards["segments"]], ["board-02"])
        self.assertEqual(boards["segments"][0]["images"], ["char-b.png", "char-a.png", "env-room.png"])
        self.assertIn("拼图", boards["segments"][0]["extra"]["negative_prompt"])

    def test_env_only_shot_stays_text_to_image(self) -> None:
        self._write()
        materials = json.loads((self.root / "proj" / "materials.json").read_text(encoding="utf-8"))
        self.assertEqual(materials["target"], "qwen_t2i")
        ids = [s["id"] for s in materials["segments"]]
        self.assertIn("board-01", ids)
        self.assertIn("char-a", ids)          # 非派生角色直接文生图
        self.assertNotIn("char-b", ids)       # 派生角色走 derive 台账

    def test_derive_ledger_uses_parent_image(self) -> None:
        self._write()
        derive = json.loads((self.root / "proj" / "derive.json").read_text(encoding="utf-8"))
        self.assertEqual(derive["target"], "qwen_edit")
        self.assertEqual(derive["segments"][0]["images"], ["char-a.png"])

    def test_narration_ledger_skips_sound_effects(self) -> None:
        self._write()
        narration = json.loads((self.root / "proj" / "narration.json").read_text(encoding="utf-8"))
        ids = [s["id"] for s in narration["segments"]]
        self.assertEqual(ids, ["nar-01"])     # 02 的说话人是"音效"，不配音
        self.assertIn("instruct", narration["segments"][0]["extra"])

    def test_prompt_files_and_ledger_line_up(self) -> None:
        self._write()
        project = json.loads((self.root / "proj" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(project["target"], "t8bridge_9ref")
        first = project["segments"][0]
        self.assertEqual(first["duration_seconds"], 8.0)
        self.assertEqual(first["images"], ["env-room.png", "board-01.png"])
        self.assertNotIn("chain_prev", first)                 # 第一镜没有上一段
        self.assertEqual(project["segments"][1]["chain_prev"], "seg-01")   # 同一场景 → 挂锚点
        prompt_file = PROJECT_ROOT / first["prompt_file"]
        self.assertTrue(str(prompt_file).endswith("seg-01.txt"))

    def test_too_many_references_is_reported(self) -> None:
        self.shots[1].refs = ["CHAR_A", "CHAR_B", "ENV_ROOM", "ENV_ROOM", "ENV_ROOM"]
        with self.assertRaises(sl.ShotlistError) as ctx:
            self._write()
        self.assertIn("超过多图参考工作流", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
