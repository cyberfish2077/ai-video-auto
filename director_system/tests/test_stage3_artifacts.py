"""阶段 3 的单测：大纲、剧本、剧本评审、关卡停机点。

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
import make_story  # noqa: E402


class GateTest(unittest.TestCase):
    """真实停机点：上游关卡没批准就必须拒绝运行。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()

    def test_requires_approved_upstream_gate(self) -> None:
        pending = {"film_id": "x", "gates": {"A": {"status": "PENDING_HUMAN_REVIEW"}}}
        with self.assertRaises(SystemExit) as ctx:
            make_story.require_gate_approved(pending, "A", self.conf)
        message = str(ctx.exception)
        self.assertIn("PENDING_HUMAN_REVIEW", message)
        self.assertIn("review_gate.py", message, "报错必须告诉人怎么批准")

    def test_approved_gate_passes(self) -> None:
        ok = {"film_id": "x", "gates": {"A": {"status": "APPROVED"}}}
        make_story.require_gate_approved(ok, "A", self.conf)

    def test_trim_suggestions_dropped_when_policy_forbids_trim(self) -> None:
        """评审里"裁掉若干帧"的建议与用户的时长策略冲突，必须被过滤掉。"""
        review = {"issues": [
            {"scope": "c2", "severity": "轻微", "category": "时长可行性",
             "smallest_fix": "把 9 帧从 s1 的尾部裁掉，保住结尾那一拍"},
            {"scope": "c2", "severity": "严重", "category": "视觉可表现性",
             "smallest_fix": "把盖章指认换成红色别针"},
            {"scope": "c9", "severity": "严重", "category": "套路",
             "smallest_fix": "这条不属于本概念，应被忽略"},
        ]}
        fixes = make_story.review_fixes(review, "c2", self.conf)
        self.assertEqual(1, len(fixes), fixes)
        self.assertIn("红色别针", fixes[0])

    def test_record_gate_b_keeps_human_gate_closed(self) -> None:
        """返修完成后，关卡 B 只能回到"等人审"，绝不能变成已批准。"""
        state = {"film_id": "x", "gates": {"A": {"status": "APPROVED"}}}
        review = {"must_fix_before_next_stage": ["a", "b", "c"]}
        make_story.record_gate_b(state, self.conf, review, revision=2)
        gate_b = state["gates"]["B"]
        self.assertEqual(self.conf["initial_gate_state"], gate_b["status"])
        self.assertEqual(2, gate_b["revision"])
        self.assertIn("剩余必修项 3 条", gate_b["note"])
        self.assertEqual("APPROVED", state["gates"]["A"]["status"], "不许动已经批准的关卡 A")


class ArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.films_root = cfg.load_settings().path(cls.conf["film_layout"]["root_key"])
        cls.films = sorted(cls.films_root.glob("*/story_outline.json"))

    def setUp(self) -> None:
        if not self.films:
            self.skipTest("还没有任何影片产出 story_outline.json")

    def _schema(self, name: str) -> dict:
        return json.loads((cfg.DIRECTOR_SCHEMAS_DIR / name).read_text(encoding="utf-8"))

    def _nominal(self, film_dir: Path) -> float:
        """本片的目标时长从**该片自己的简报**读（不写死 30——那是上一部片的规模）。"""
        brief = json.loads((film_dir / "brief.json").read_text(encoding="utf-8"))
        return make_story.nominal_seconds_of(brief, self.conf)

    def test_outline_matches_schema_and_duration(self) -> None:
        schema = self._schema("story_outline.schema.json")
        for path in self.films:
            with self.subTest(film=path.parent.name):
                outline = json.loads(path.read_text(encoding="utf-8"))
                ideas = json.loads((path.parent / "ideas.json").read_text(encoding="utf-8"))
                concept = make_story.find_concept(ideas, outline["concept_id"])
                self.assertEqual([], make_ideas.schema_errors(outline, schema))
                self.assertEqual([], make_story.duration_problems(
                    outline, self._nominal(path.parent), self.conf))
                engines = [s["dramatic_engine"] for s in outline["scenes"]]
                self.assertTrue(all(engines), "每场戏都必须标出戏剧引擎")

    def test_screenplay_matches_schema_and_duration(self) -> None:
        schema = self._schema("screenplay.schema.json")
        for path in (p.parent / "screenplay.json" for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                screenplay = json.loads(path.read_text(encoding="utf-8"))
                ideas = json.loads((path.parent / "ideas.json").read_text(encoding="utf-8"))
                concept = make_story.find_concept(ideas, screenplay["concept_id"])
                self.assertEqual([], make_ideas.schema_errors(screenplay, schema))
                self.assertEqual([], make_story.duration_problems(
                    screenplay, self._nominal(path.parent), self.conf))
                # 场次数量由戏剧节拍决定（不为了凑生成段数而平均切）——只要求至少两场，
                # 以及各场之和与 total_seconds 自洽（后者由 duration_problems 守着）。
                self.assertGreaterEqual(len(screenplay["scenes"]), 2)
                self.assertGreaterEqual(len(screenplay["review_checklist"]), 3)
                # 台词与字幕风险必须显式交代
                self.assertTrue(screenplay["dialogue_policy_note"].strip())

    def test_screenplay_review_never_auto_approves(self) -> None:
        schema = self._schema("artifact_review.schema.json")
        for path in (p.parent / "screenplay_review.json" for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                review = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([], make_ideas.schema_errors(review, schema))
                self.assertEqual("screenplay", review["target"])
                self.assertEqual("PENDING_HUMAN_REVIEW", review["status"])
                self.assertFalse(review["auto_approve"])

    def test_state_records_choice_and_waits_at_gate_b(self) -> None:
        declared = set(self.conf["gate_states"])
        for path in (p.parent / str(self.conf["film_layout"]["state_file"]) for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                state = json.loads(path.read_text(encoding="utf-8"))
                gate_a = state["gates"]["A"]
                self.assertEqual("APPROVED", gate_a["status"])
                self.assertTrue(gate_a.get("chosen_concept"), "必须记住选了哪个概念")
                self.assertIn(state["gates"]["B"]["status"], declared)
                self.assertTrue(state.get("history"), "关卡改动必须留痕")
                # 任何被批准的关卡都必须留下"谁、什么时候、为什么"的记录，
                # 以此证明是走 review_gate.py 由人批的，而不是生成脚本自己写上去的。
                for gate_id, entry in state["gates"].items():
                    if entry["status"] != "APPROVED":
                        continue
                    self.assertTrue(entry.get("decided_at"), f"关卡 {gate_id} 缺少批准时间")
                    self.assertTrue(entry.get("decided_by"), f"关卡 {gate_id} 缺少批准人")


if __name__ == "__main__":
    unittest.main()
