"""阶段 4 的单测：分镜验证、资产口径、空间平面图与镜内分拍。

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


class HelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        film = PROJECT_ROOT / "projects" / "animal-heads-nap-01"
        ideas = json.loads((film / "ideas.json").read_text(encoding="utf-8"))
        cls.concept = make_story.find_concept(ideas, "c2")

    def test_locked_concept_drops_superseded_prose(self) -> None:
        """概念里对分段内容的旧文字已被剧本取代，不许再下发给模型。"""
        locked = make_storyboard.locked_concept(self.concept)
        blob = json.dumps(locked, ensure_ascii=False)
        for term in make_storyboard.SUPERSEDED_TERMS:
            self.assertNotIn(term, blob, f"锁定输入里不该出现旧说法「{term}」")
        self.assertNotIn("content", blob, "概念的分段文字（content）不该下发")
        self.assertIn("pacing_intent", blob, "节奏意图必须下发（分段在分镜阶段才定）")

    def test_constraints_only_carry_frames(self) -> None:
        text = make_storyboard.storyboard_constraints(self.conf, 30.0)
        self.assertIn("243 帧", text)
        for term in make_storyboard.SUPERSEDED_TERMS:
            self.assertNotIn(term, text, f"约束文本里不该出现旧说法「{term}」")

    def _base_board(self) -> dict:
        film = PROJECT_ROOT / "projects" / "animal-heads-nap-01"
        return json.loads((film / "storyboard.json").read_text(encoding="utf-8"))

    def test_checker_rejects_stale_terms(self) -> None:
        board = copy.deepcopy(self._base_board())
        board["shots"][0]["composition"] += "（沿用鹰爪主管的旧设定）"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("旧说法" in p for p in problems), problems)

    def test_checker_rejects_late_final_beat(self) -> None:
        board = copy.deepcopy(self._base_board())
        board["shots"][0]["internal_beats"][-1]["at_seconds"] = 9.9
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("演不完" in p for p in problems), problems)

    def test_checker_rejects_prompt_written_too_early(self) -> None:
        board = copy.deepcopy(self._base_board())
        board["shots"][0]["prompt"] = "阶段 5 才该写的东西"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("不许写提示词" in p for p in problems), problems)

    def test_checker_rejects_unknown_asset(self) -> None:
        board = copy.deepcopy(self._base_board())
        board["shots"][0]["reference_assets"][0]["asset_id"] = "AS-NOT-EXIST"
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertTrue(any("没有登记在 asset_registry" in p for p in problems), problems)

    def test_checker_requires_space_plan_for_every_shot(self) -> None:
        board = copy.deepcopy(self._base_board())
        board["space_plan"]["camera_positions"] = board["space_plan"]["camera_positions"][:1]
        problems = make_storyboard.storyboard_problems(board, self.conf, 30.0)
        self.assertGreaterEqual(len([p for p in problems if "机位条目" in p]), 2)


class ArtifactTest(unittest.TestCase):
    #: 同一件事只允许一种说法。左边是**不该再出现**的互斥措辞——
    #: 每一条都来自真实评审（第 3 版分镜的必修项），由 Codex 手工对齐后钉在这里，
    #: 免得以后重新生成分镜时又被搬回来。以后评审再发现同类问题，继续往这里加。
    FORBIDDEN_WORDING = [
        ("隔断另一侧", "键盘声来源与「画框外、不入画」互斥"),
        ("就在他隔壁", "把键盘声源绑在明确相邻工位，与「画框外」互斥"),
        ("桌面中央", "别针回推落点必须写死为「近端槽口内侧」"),
        ("桌子中央", "同上"),
        ("反手把织带上那枚红别针摘下", "摘针已移到 7.8 拍位移途中，不应再与架人同拍"),
        ("先把实习生的虚焦肩背接走", "接走已改为「自画框左缘进出、被前景遮挡、只给结果帧」"),
    ]

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.films_root = cfg.load_settings().path(cls.conf["film_layout"]["root_key"])
        cls.films = sorted(cls.films_root.glob("*/storyboard.json"))

    def setUp(self) -> None:
        if not self.films:
            self.skipTest("还没有任何影片产出 storyboard.json")

    def test_storyboard_matches_schema_and_verification(self) -> None:
        schema = json.loads((cfg.DIRECTOR_SCHEMAS_DIR / "storyboard.schema.json").read_text(encoding="utf-8"))
        for path in self.films:
            with self.subTest(film=path.parent.name):
                board = json.loads(path.read_text(encoding="utf-8"))
                ideas = json.loads((path.parent / "ideas.json").read_text(encoding="utf-8"))
                concept = make_story.find_concept(ideas, board["concept_id"])
                self.assertEqual([], make_ideas.schema_errors(board, schema))
                # 本片的目标时长从该片自己的简报读（不写死 30——那是上一部片的规模）
                brief = json.loads((path.parent / "brief.json").read_text(encoding="utf-8"))
                nominal = make_story.nominal_seconds_of(brief, self.conf)
                self.assertEqual([], make_storyboard.storyboard_problems(board, self.conf, nominal))
                # 一镜＝一段：镜头 ID 与生成段 ID 都必须唯一
                ids = [s["shot_id"] for s in board["shots"]]
                self.assertEqual(len(ids), len(set(ids)), "镜头 ID 必须唯一")

    def test_storyboard_review_never_auto_approves(self) -> None:
        schema = json.loads((cfg.DIRECTOR_SCHEMAS_DIR / "artifact_review.schema.json").read_text(encoding="utf-8"))
        for path in (p.parent / "storyboard_review.json" for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                review = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([], make_ideas.schema_errors(review, schema))
                self.assertEqual("storyboard", review["target"])
                self.assertEqual("PENDING_HUMAN_REVIEW", review["status"])
                self.assertFalse(review["auto_approve"])

    def test_state_waits_at_gate_c(self) -> None:
        declared = set(self.conf["gate_states"])
        for path in (p.parent / str(self.conf["film_layout"]["state_file"]) for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                state = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual("APPROVED", state["gates"]["A"]["status"])
                # 制作前确认关：可以是"等人审"（片子在流程中），一旦批准就必须有时间与批准人；
                # 任何取值都必须在配置声明的状态里（不许随手写状态名）。
                gate_b = state["gates"]["B"]
                self.assertIn(gate_b["status"], declared)
                if gate_b["status"] == "APPROVED":
                    self.assertTrue(gate_b.get("decided_at"), "制作前确认关缺少批准时间")
                    self.assertTrue(gate_b.get("decided_by"), "制作前确认关缺少批准人")
                # 成片质检关（新 C）只能由人批准；生成脚本只能把它置为"等人审"。
                self.assertIn(state["gates"]["C"]["status"], declared)
                if state["gates"]["C"]["status"] == "APPROVED":
                    entry = state["gates"]["C"]
                    self.assertTrue(entry.get("decided_at"), "成片质检关缺少批准时间")
                    self.assertTrue(entry.get("decided_by"), "成片质检关缺少批准人")

    def test_no_contradictory_wording(self) -> None:
        for path in self.films:
            with self.subTest(film=path.parent.name):
                blob = path.read_text(encoding="utf-8")
                for term, why in self.FORBIDDEN_WORDING:
                    self.assertNotIn(term, blob, f"分镜里又出现了互斥措辞「{term}」：{why}")


if __name__ == "__main__":
    unittest.main()
