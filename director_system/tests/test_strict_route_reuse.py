"""严格版（路线 A）的阶段复用：**不调模型、不花显卡**。

守的是用户 2026-10-15 的要求"阶段复用/续跑"：四个阶段是串行的，上一阶段通过、
下一阶段失败时，原来的路只有两条——`--force` 整段重做（多烧调用）或停下等人判断。
现在改成"**通过就复用、只补缺的那一段**"。

做法：把一部**真片子**（默认 nap-02，已交付、三道闸门都已批准）的文本产物复制到临时目录，
把 `paths.projects` 指到那里，用"一调用就报错"的假 DeepSeek 客户端跑四个阶段：

* 四个阶段都必须退出码 0；
* **一次模型调用都不许发生**（调用了就直接判失败）；
* 复盘产物：除 `state.json` 的更新时间外，其余文本产物**逐字节不变**；
* 三道闸门的批准状态不许被打回（`record_gate` 的铁律）。

运行方式（在项目根目录）::

    .venv\\Scripts\\python.exe -m unittest discover -s director_system/tests -t director_system/tests
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(DIRECTOR_ROOT / "scripts"))

from video_auto import config as cfg  # noqa: E402
from video_auto import deepseek as ds  # noqa: E402

import make_ideas  # noqa: E402
import make_prompts  # noqa: E402
import make_story  # noqa: E402
import make_storyboard  # noqa: E402

#: 用哪部已交付的真片子当夹具
FIXTURE_FILM = "animal-heads-nap-02"
#: 临时目录里的片名**必须与夹具同名**：产物内部记着 film_id（`superseded_terms.json`、
#: `load_voice_policy` 等都按它去找本片文件），改个名字会让校验按"默认表"判，出现假问题。
#: 临时根目录是独立的，所以不会碰到真片子。
TEST_FILM = FIXTURE_FILM

#: 四个阶段各自需要哪些文本产物（stage 名 → 文件名列表）
STAGE_ARTIFACTS = {
    "ideas": ["brief.json", "ideas.json", "ideas.md",
              "idea_review.json", "idea_review.md", "state.json"],
    "story": ["brief.json", "ideas.json", "state.json",
              "story_outline.json", "story_outline.md",
              "screenplay.json", "screenplay.md",
              "screenplay_review.json", "screenplay_review.md"],
    "storyboard": ["brief.json", "ideas.json", "screenplay.json", "screenplay_review.json",
                   "state.json", "storyboard.json", "storyboard.md",
                   "storyboard_review.json", "storyboard_review.md"],
    "prompts": ["brief.json", "ideas.json", "screenplay.json", "storyboard.json",
                "storyboard_review.json", "state.json",
                "prompt_pack.json", "prompt_pack.md",
                "prompt_review.json", "prompt_review.md"],
}


class _ExplodingClient:
    """一被调用就炸的假客户端：用来证明"复用路径一次模型调用都没有"。"""

    def __init__(self, *args, **kwargs):
        self.model = "stub"
        self.last_finish_reason = None
        self.last_usage: dict = {}
        self.settings = args[0] if args else None

    def chat(self, *args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("复用路径不该调用 DeepSeek（说明复用没生效，白花钱了）")


def dir_fingerprint(root: Path, *, skip: set[str] | None = None) -> dict[str, str]:
    skip = skip or set()
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in skip:
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class StrictReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = cfg.load_settings().path("paths.projects") / FIXTURE_FILM
        if not (self.src / "prompt_pack.json").is_file():
            self.skipTest(f"需要已交付片子的文本产物做夹具：{self.src}")
        self._tmp = tempfile.TemporaryDirectory(prefix="video-auto-strict-")
        self.addCleanup(self._tmp.cleanup)
        self.projects_root = Path(self._tmp.name)
        self.film_dir = self.projects_root / TEST_FILM
        self.film_dir.mkdir(parents=True)
        self._copy_artifacts()
        real = cfg.load_settings()
        raw = json.loads(json.dumps(real.raw))
        raw.setdefault("paths", {})["projects"] = str(self.projects_root)
        self.settings = cfg.Settings(raw=raw, source=real.source)
        patch = mock.patch.object(cfg, "load_settings", lambda: self.settings)
        patch.start()
        self.addCleanup(patch.stop)
        patch_client = mock.patch.object(ds, "DeepSeekClient", _ExplodingClient)
        patch_client.start()
        self.addCleanup(patch_client.stop)
        self.before = dir_fingerprint(self.film_dir, skip={"state.json"})

    def _copy_artifacts(self) -> None:
        # superseded_terms.json 也要带上：它是"本片旧说法禁用表"，不带上会让分镜/提示词
        # 校验按**默认表**判，把本片合法的说法（nap-02 的"镜面反光"）误判成问题。
        wanted = set().union(*STAGE_ARTIFACTS.values()) | {"superseded_terms.json"}
        for name in sorted(wanted):
            source = self.src / name
            if source.is_file():
                shutil.copy2(source, self.film_dir / name)
        prompts_dir = self.src / "prompts"
        if prompts_dir.is_dir():
            shutil.copytree(prompts_dir, self.film_dir / "prompts")

    def run_stage(self, module, extra: list[str]) -> tuple[int, str]:
        argv = [str(getattr(module, "__file__")), "--film-id", TEST_FILM] + extra
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buffer):
            code = module.main()
        return code, buffer.getvalue()

    def test_ideas_stage_reuses_and_reports_gate_a(self) -> None:
        code, output = self.run_stage(
            make_ideas, ["--brief-file", str(self.film_dir / "brief.json")])
        self.assertEqual(code, 0, output)
        self.assertIn("复用已有 ideas.json", output)
        self.assertIn("复用已有 idea_review.json", output)
        self.assertIn("本次没有调用 DeepSeek", output)

    def test_story_stage_reuses_all_three(self) -> None:
        code, output = self.run_stage(make_story, [])
        self.assertEqual(code, 0, output)
        for name in ("story_outline.json", "screenplay.json", "screenplay_review.json"):
            self.assertIn(f"复用已有 {name}", output)
        self.assertIn("本次没有调用 DeepSeek", output)

    def test_storyboard_stage_reuses_all_two(self) -> None:
        code, output = self.run_stage(make_storyboard, [])
        self.assertEqual(code, 0, output)
        self.assertIn("复用已有 storyboard.json", output)
        self.assertIn("复用已有 storyboard_review.json", output)

    def test_prompts_stage_reuses_pack_and_review(self) -> None:
        code, output = self.run_stage(make_prompts, ["--style", "photoreal-cinematic"])
        self.assertEqual(code, 0, output)
        self.assertIn("复用已有 prompt_pack.json", output)
        self.assertIn("提示词包已有且校验通过", output)
        self.assertIn("本次没有调用 DeepSeek", output)

    def test_all_four_stages_leave_artifacts_untouched(self) -> None:
        for module, extra in ((make_ideas, ["--brief-file", str(self.film_dir / "brief.json")]),
                              (make_story, []), (make_storyboard, []),
                              (make_prompts, ["--style", "photoreal-cinematic"])):
            code, output = self.run_stage(module, extra)
            self.assertEqual(code, 0, f"{module.__name__}：{output}")
        after = dir_fingerprint(self.film_dir, skip={"state.json"})
        self.assertEqual(self.before, after, "复用路径不该改动任何已有产物")

    def test_approved_gates_are_not_pushed_back(self) -> None:
        for module, extra in ((make_ideas, ["--brief-file", str(self.film_dir / "brief.json")]),
                              (make_story, []), (make_storyboard, []),
                              (make_prompts, ["--style", "photoreal-cinematic"])):
            code, output = self.run_stage(module, extra)
            self.assertEqual(code, 0, f"{module.__name__}：{output}")
        state = json.loads((self.film_dir / "state.json").read_text(encoding="utf-8"))
        for gate in ("A", "B", "C"):
            self.assertEqual("APPROVED", (state["gates"].get(gate) or {}).get("status"),
                             f"重跑阶段脚本把闸门 {gate} 打回了，违反 record_gate 的铁律")


class MustFixScopeTest(unittest.TestCase):
    """必修项口径（用户 2026-10-15 定）：只有致命/严重能进 must_fix，轻微只记录。"""

    @staticmethod
    def review(fatal: int, serious: int, minor: int, must_fix: int) -> dict:
        issues: list[dict] = []
        for severity, count in (("致命", fatal), ("严重", serious), ("轻微", minor)):
            for index in range(count):
                issues.append({"scope": "SC01", "category": "因果", "severity": severity,
                               "evidence": f"证据 {index}（至少十个字符）",
                               "smallest_fix": "最小修法"})
        return {"issues": issues,
                "must_fix_before_next_stage": [f"必修 {i}" for i in range(must_fix)]}

    def test_flags_minor_items_smuggled_into_must_fix(self) -> None:
        problems = make_ideas.must_fix_scope_problems(self.review(1, 1, 6, 5))
        self.assertEqual(1, len(problems))
        self.assertIn("轻微", problems[0])
        self.assertIn("最多允许 2 条", problems[0])

    def test_accepts_must_fix_within_fatal_and_serious(self) -> None:
        self.assertEqual([], make_ideas.must_fix_scope_problems(self.review(1, 1, 6, 2)))
        self.assertEqual([], make_ideas.must_fix_scope_problems(self.review(2, 0, 3, 1)))
        self.assertEqual([], make_ideas.must_fix_scope_problems(self.review(0, 0, 4, 0)))

    def test_prompt_and_schema_state_the_rule(self) -> None:
        prompt = (DIRECTOR_ROOT / "prompts" / "06_quality_review.md").read_text(encoding="utf-8")
        self.assertIn("must_fix_before_next_stage", prompt)
        self.assertIn("Never put a 轻微 item there", prompt)
        schema = json.loads((DIRECTOR_ROOT / "schemas" / "artifact_review.schema.json")
                            .read_text(encoding="utf-8"))
        described = schema["properties"]["must_fix_before_next_stage"]["description"]
        self.assertIn("只收致命与严重", described)

    def test_every_review_call_is_wired(self) -> None:
        """四个阶段的每一处评审调用都要带这条校验（否则模型又可以往回塞轻微项）。"""
        expected = {"make_ideas.py": 1, "make_story.py": 2, "make_storyboard.py": 2,
                    "make_prompts.py": 2}
        for name, least in expected.items():
            source = (DIRECTOR_ROOT / "scripts" / name).read_text(encoding="utf-8")
            found = source.count("extra_check=make_ideas.must_fix_scope_problems") + \
                source.count("extra_check=must_fix_scope_problems")
            self.assertGreaterEqual(found, least, f"{name} 有评审调用没接上必修项口径校验")


class UpstreamGuardTest(unittest.TestCase):
    """上游版本对不上就不许复用（否则会把基于旧剧本的分镜当成有效的）。"""

    def test_guard_flags_missing_and_mismatched_fields(self) -> None:
        guard = make_ideas.upstream_guard("storyboard_revision", 3)
        self.assertEqual([], guard({"storyboard_revision": 3}))
        self.assertTrue(guard({}), "缺字段要保守地判为不可复用")
        self.assertTrue(guard({"storyboard_revision": 2}), "版本不一致必须判为不可复用")

    def test_reusable_artifact_removes_reason_when_check_fails(self) -> None:
        import tempfile as tf
        with tf.TemporaryDirectory(prefix="video-auto-reuse-") as tmp:
            path = Path(tmp) / "x.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            self.assertIsNone(make_ideas.reusable_artifact(
                path, lambda d: ["故意失败"], what="测试"))
            self.assertEqual({"a": 1}, make_ideas.reusable_artifact(path, lambda d: [], what="测试"))
            self.assertIsNone(make_ideas.reusable_artifact(Path(tmp) / "缺.json", None))


if __name__ == "__main__":
    unittest.main()
