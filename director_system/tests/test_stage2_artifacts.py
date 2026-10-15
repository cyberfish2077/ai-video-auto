"""阶段 2 的单测：成果文件、分段算式、审核关卡。

运行方式（在项目根目录）::

    .venv\\Scripts\\python.exe -m unittest discover -s director_system/tests -t director_system/tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(DIRECTOR_ROOT / "scripts"))

from video_auto import config as cfg  # noqa: E402

import make_ideas  # noqa: E402


class ToolTest(unittest.TestCase):
    """脚本里的纯函数（不联网）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()

    def test_legal_frame_table_is_authoritative(self) -> None:
        """帧数是权威值：15.08 秒必须是 362 帧，不能用"四舍五入的秒×24"反算成 361。"""
        table = make_ideas.legal_frame_table(self.conf)
        frames = [n for n, _ in table]
        spec = self.conf["generation"]["frames"]
        self.assertEqual(spec["min"], frames[0])
        self.assertEqual(spec["max"], frames[-1])
        self.assertEqual(362, frames[-1])
        self.assertAlmostEqual(15.08, table[-1][1], places=2)
        for n in frames:
            self.assertEqual(spec["min_mod"], n % spec["step"], f"{n} 不是 17k+5")

    def test_extract_json_tolerates_fences_and_noise(self) -> None:
        self.assertEqual({"a": 1}, make_ideas.extract_json('```json\n{"a": 1}\n```'))
        self.assertEqual({"a": 1}, make_ideas.extract_json('好的，结果如下：\n{"a": 1}\n希望有帮助'))
        with self.assertRaises(ValueError):
            make_ideas.extract_json("这里没有 JSON")

class ArtifactTest(unittest.TestCase):
    """磁盘上的阶段 2 成果必须仍然合规（换模型重跑后也守得住）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.films_root = cfg.load_settings().path(cls.conf["film_layout"]["root_key"])
        cls.films = sorted(p for p in cls.films_root.glob("*/ideas.json"))

    def setUp(self) -> None:
        if not self.films:
            self.skipTest("还没有任何影片产出 ideas.json")

    def test_ideas_match_schema_and_frame_math(self) -> None:
        schema = json.loads((cfg.DIRECTOR_SCHEMAS_DIR / "idea_proposal.schema.json")
                            .read_text(encoding="utf-8"))
        for path in self.films:
            with self.subTest(film=path.parent.name):
                ideas = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([], make_ideas.schema_errors(ideas, schema))
                self.assertEqual(5, len(ideas["concepts"]))
                self.assertEqual(2, len(ideas["recommendation"]["recommended_ids"]))

    def test_review_matches_schema_and_never_auto_approves(self) -> None:
        schema = json.loads((cfg.DIRECTOR_SCHEMAS_DIR / "idea_review.schema.json")
                            .read_text(encoding="utf-8"))
        for path in (p.parent / "idea_review.json" for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                review = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([], make_ideas.schema_errors(review, schema))
                self.assertEqual("PENDING_HUMAN_REVIEW", review["status"])
                self.assertEqual(5, len(review["per_concept"]))
                self.assertGreaterEqual(len(review["issues"]), 3)

    def test_state_uses_declared_gate_states_only(self) -> None:
        declared = set(self.conf["gate_states"]) | {"NOT_STARTED"}
        for path in (p.parent / str(self.conf["film_layout"]["state_file"]) for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                state = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    {"A", "B", "C"}, set(state["gates"]), "三个审核关卡必须齐全（A 选案 / B 制作前确认 / C 成片质检）"
                )
                for gate_id, entry in state["gates"].items():
                    self.assertIn(entry["status"], declared, f"关卡 {gate_id} 状态未在配置里声明")
                # 被批准的关卡必须留下"人在什么时候用什么理由批的"记录，
                # 以此证明是走 review_gate.py 批的，而不是脚本自己写上去的。
                for gate_id, entry in state["gates"].items():
                    if entry["status"] != "APPROVED":
                        continue
                    self.assertTrue(entry.get("decided_at"), f"关卡 {gate_id} 缺少批准时间")
                    self.assertTrue(entry.get("decided_by"), f"关卡 {gate_id} 缺少批准人")
                    self.assertTrue(
                        any(h.get("gate") == gate_id and h.get("status") == "APPROVED"
                            for h in state.get("history") or []),
                        f"关卡 {gate_id} 的批准没有写进 history（无法追溯）",
                    )

    def test_review_gate_show_runs(self) -> None:
        film_id = self.films[0].parent.name
        result = subprocess.run(
            [sys.executable, str(DIRECTOR_ROOT / "scripts" / "review_gate.py"),
             "--film-id", film_id, "--show"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # 只要求"三个关卡都列出来"，不依赖某一关当前的状态
        # （关卡会随人工批准往前走，硬编码状态会让测试过时）
        for gate_id, title in (("A", "创意提案"), ("B", "制作前确认"), ("C", "成片质检")):
            self.assertIn(gate_id, result.stdout)
            self.assertIn(title, result.stdout)

    def test_review_gate_refuses_unknown_gate(self) -> None:
        film_id = self.films[0].parent.name
        result = subprocess.run(
            [sys.executable, str(DIRECTOR_ROOT / "scripts" / "review_gate.py"),
             "--film-id", film_id, "--approve", "Z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("没有这个关卡", result.stdout)

    def test_brief_records_user_input_and_assumptions(self) -> None:
        for path in (p.parent / "brief.json" for p in self.films):
            if not path.is_file():
                continue
            with self.subTest(film=path.parent.name):
                brief = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("raw_quote", brief["from_user"], "用户原话必须留档")
                self.assertIn("assumed_by_codex", brief, "Codex 补的默认值必须标注出来")


if __name__ == "__main__":
    unittest.main()
