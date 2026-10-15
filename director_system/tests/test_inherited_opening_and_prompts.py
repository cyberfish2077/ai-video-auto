"""沿用段（开篇继承既有素材）与"提示词拼装函数必须可调用"的回归测试。

两件事各守一类真实事故：

1. **提示词拼装函数必须真的能跑**。2026-10-14 抓到过一处：分镜的三个调用点
   （`storyboard_constraints(conf, concept)`）把概念字典当成秒数传进去，
   一旦真跑 `make_storyboard.py` 就 TypeError，而单测只直接调用该函数、绕过了调用点，
   于是"测试全过、流程一跑就崩"。这里改为**端到端调用所有拼装函数**。
2. **沿用段只算时长、不进镜头表**。开篇沿用的既有成片不在本片重拍，
   所以它不写进场次/镜头列表，只用片级 `inherited_seconds` 计入全片时长；
   校验不变式是"各场／各镜之和 + inherited_seconds = total_seconds"。

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
import make_narration  # noqa: E402
import make_prompts  # noqa: E402
import make_story  # noqa: E402
import make_storyboard  # noqa: E402

NAP01 = PROJECT_ROOT / "projects" / "animal-heads-nap-01"
NAP02_BRIEF = PROJECT_ROOT / "projects" / "animal-heads-nap-02" / "brief.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PromptBuildersTest(unittest.TestCase):
    """所有拼装函数都要能用真实成果跑通（守住"测试全过、流程一崩"）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.brief = _load(NAP02_BRIEF)
        cls.ideas = _load(NAP01 / "ideas.json")
        cls.concept = cls.ideas["concepts"][0]
        cls.outline = _load(NAP01 / "story_outline.json")
        cls.screenplay = _load(NAP01 / "screenplay.json")
        cls.screenplay_review = _load(NAP01 / "screenplay_review.json")
        cls.board = _load(NAP01 / "storyboard.json")
        cls.storyboard_review = _load(NAP01 / "storyboard_review.json")
        cls.idea_schema = make_ideas.load_schema("idea_proposal.schema.json")
        cls.idea_review_schema = make_ideas.load_schema("idea_review.schema.json")
        cls.story_schema = make_ideas.load_schema("story_outline.schema.json")
        cls.board_schema = make_ideas.load_schema("storyboard.schema.json")
        cls.pack_schema = make_ideas.load_schema("prompt_pack.schema.json")
        cls.review_schema = make_ideas.load_schema("artifact_review.schema.json")
        profile_name, profile = cfg.load_style("photoreal-cinematic")
        cls.style_name = profile_name
        cls.style_prefix = str(profile.get("style_prefix") or "")

    def test_every_stage_prompt_builds(self) -> None:
        """每个阶段的 user prompt 都必须能拼出来（不许再出现"调用点传错参数"）。"""
        texts = {
            "创意提案": make_ideas.ideas_user_prompt(self.brief, self.conf, self.idea_schema),
            "创意评审": make_ideas.review_user_prompt(
                self.brief, self.ideas, self.conf, self.idea_review_schema),
            "大纲": make_story.outline_user_prompt(
                self.brief, self.concept, [], self.conf, self.story_schema),
            "剧本": make_story.screenplay_user_prompt(
                self.brief, self.concept, self.outline, [], self.conf, self.story_schema),
            "剧本评审": make_story.review_user_prompt(
                self.screenplay, self.conf, self.concept, self.review_schema, brief=self.brief),
            "分镜": make_storyboard.storyboard_user_prompt(
                self.brief, self.concept, self.screenplay, self.screenplay_review, "",
                self.conf, self.board_schema),
            "分镜评审": make_storyboard.review_user_prompt(
                self.board, self.conf, self.concept, self.review_schema, self.brief),
            "分镜返修": make_storyboard.revision_user_prompt(
                self.brief, self.concept, self.board, self.storyboard_review,
                ["示例必修项"], 2, self.conf, self.board_schema),
            "提示词编译": make_prompts.compile_user_prompt(
                self.brief, self.board, self.storyboard_review, "", self.style_prefix,
                self.style_name, self.conf, self.pack_schema),
            "提示词评审": make_prompts.review_user_prompt(
                _load(NAP01 / "prompt_pack.json"), self.conf, self.board, self.review_schema),
        }
        for label, text in texts.items():
            with self.subTest(label=label):
                self.assertIsInstance(text, str)
                self.assertGreater(len(text), 200, f"{label} 的提示词太短，可能是拼装失败")

    def test_storyboard_constraints_rejects_dict_as_nominal(self) -> None:
        """把非数字传进 nominal 时必须立刻报错，而不是悄悄拼出错误文本。"""
        with self.assertRaises(TypeError):
            make_storyboard.storyboard_constraints(self.conf, self.concept)  # type: ignore[arg-type]


class DialoguePolicyTest(unittest.TestCase):
    """台词/旁白：允许写进剧本与提示词包的 `dialogue`，但**文字不许进画面提示词**，由后期配音实现。"""

    def test_pack_schema_allows_dialogue(self) -> None:
        schema = make_ideas.load_schema("prompt_pack.schema.json")
        shot = schema["properties"]["shots"]["items"]
        self.assertIn("dialogue", shot["properties"])
        self.assertNotIn("const", shot["properties"]["dialogue_lines"])

    def test_dialogue_text_must_not_leak_into_prompt(self) -> None:
        """策略驱动：`dialogue: tts` 时台词文字不许进画面提示词；默认 `h3`（让视频模型说）时允许。"""
        conf = cfg.load_director_config()
        board = json.loads((PROJECT_ROOT / "projects" / "animal-heads-nap-02" / "storyboard.json")
                           .read_text(encoding="utf-8"))
        pack = json.loads((PROJECT_ROOT / "projects" / "animal-heads-nap-02" / "prompt_pack.json")
                          .read_text(encoding="utf-8"))
        pack["shots"][0]["dialogue"] = [{"speaker": "旁白", "text": "这是一句不该出现的话"}]
        pack["shots"][0]["dialogue_lines"] = 1
        pack["shots"][0]["prompt_lines"]["timeline"][0] = "0-2秒：他开口说这是一句不该出现的话。"
        style = pack["shots"][0]["prompt_lines"]["style_line"][:12]
        # 默认策略 dialogue=h3：台词进画面是**有意为之**（视频模型要靠它说出口），不该报警
        self.assertEqual("h3", make_prompts.DEFAULT_VOICE_POLICY["dialogue"])
        problems = make_prompts.prompt_problems({**pack, "_style_prefix": style}, board, conf)
        self.assertFalse(any("画面提示词" in p for p in problems), problems)
        # 明确要求走 TTS 时：同样的写法必须被拦下
        original = make_prompts.load_voice_policy
        try:
            make_prompts.load_voice_policy = lambda *a, **k: {"dialogue": "tts", "narration": "tts"}
            problems = make_prompts.prompt_problems({**pack, "_style_prefix": style}, board, conf)
        finally:
            make_prompts.load_voice_policy = original
        self.assertTrue(any("台词" in p and "画面提示词" in p for p in problems), problems)

    def test_narration_ledger_from_dialogue(self) -> None:
        """台词条目 → 配音台账：id 用 nar-<段id>、同一镜多条合并、音色取 voice_note。"""
        film = PROJECT_ROOT / "scratch" / "fake-film"
        (film).mkdir(parents=True, exist_ok=True)
        pack = {"shots": [
            {"shot_id": "SH01", "segment_id": "s1",
             "dialogue": [{"speaker": "旁白", "text": "第一句", "voice_note": "低沉男声"}],
             "dialogue_lines": 1},
            {"shot_id": "SH02", "segment_id": "s2", "dialogue": [], "dialogue_lines": 0},
            {"shot_id": "SH03", "segment_id": "s3",
             "dialogue": [{"speaker": "主角", "text": "第二句"}, {"speaker": "主角", "text": "第三句"}],
             "dialogue_lines": 2},
        ]}
        (film / "prompt_pack.json").write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        (film / "brief.json").write_text(json.dumps({"voices": {"主角": "沙哑的中年男声"}}, ensure_ascii=False),
                                         encoding="utf-8")
        ledger, warnings = make_narration.build_ledger("fake-film", film)
        ids = [seg["id"] for seg in ledger["segments"]]
        self.assertEqual(["nar-s1", "nar-s3"], ids)
        self.assertEqual("第一句", ledger["segments"][0]["prompt"])
        self.assertEqual("低沉男声", ledger["segments"][0]["extra"]["instruct"])
        self.assertEqual("第二句，第三句", ledger["segments"][1]["prompt"])
        self.assertEqual("沙哑的中年男声", ledger["segments"][1]["extra"]["instruct"])
        self.assertEqual("tts_voice_design", ledger["target"])
        self.assertEqual([], warnings)


class RevisionCheckTest(unittest.TestCase):
    """返修用的机械校验必须是"自洽函数"（2026-10-15 的三次崩溃都出在内联 lambda 的自由变量上）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.outline = _load(NAP01 / "story_outline.json")
        cls.board = _load(NAP01 / "storyboard.json")
        cls.screenplay = _load(NAP01 / "screenplay.json")

    def test_revision_checks_is_callable(self) -> None:
        nominal = float(self.outline["total_seconds"])
        problems = make_story.revision_checks(self.outline, nominal, self.conf, 0)
        self.assertIsInstance(problems, list)

    def test_revision_board_checks_is_callable(self) -> None:
        nominal = float(self.board["total_seconds"])
        bible = self.screenplay.get("continuity_bible") or {}
        problems = make_storyboard.revision_board_checks(self.board, nominal, self.conf, bible, 0)
        self.assertIsInstance(problems, list)


class InheritedOpeningTest(unittest.TestCase):
    """开篇沿用既有素材：只算时长、不进镜头表。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.brief = _load(NAP02_BRIEF)
        cls.board = _load(NAP01 / "storyboard.json")
        cls.inherited = float(cls.brief["inherited_opening"]["seconds"])

    def test_brief_declares_structured_inherited_opening(self) -> None:
        block = make_ideas.inherited_opening(self.brief)
        self.assertIsNotNone(block)
        self.assertEqual(block["source_film"], "animal-heads-nap-01")
        self.assertAlmostEqual(float(block["seconds"]), 30.375, places=3)
        self.assertEqual(block["segment_ids"], ["s1", "s2", "s3"])

    def test_constraints_text_states_the_numbers(self) -> None:
        text = make_ideas.inherited_opening_constraints(self.brief)
        self.assertIn("30.375", text)
        self.assertIn("inherited_seconds", text)
        self.assertIn("不要", text)
        self.assertIn("120", text)
        # 没有沿用段时不许出现这段约束
        self.assertEqual(make_ideas.inherited_opening_constraints({"film_id": "x"}), "")

    def test_storyboard_invariant_accepts_inherited(self) -> None:
        board = copy.deepcopy(self.board)
        shots_seconds = round(sum(s["generation_frames"] for s in board["shots"]) / 24, 3)
        board["inherited_seconds"] = shots_seconds
        board["total_seconds"] = round(shots_seconds * 2, 3)
        problems = make_storyboard.storyboard_problems(
            board, self.conf, float(board["total_seconds"]), None)
        self.assertFalse(
            any("不一致" in p for p in problems), problems)

    def test_storyboard_invariant_rejects_missing_inherited(self) -> None:
        board = copy.deepcopy(self.board)
        shots_seconds = round(sum(s["generation_frames"] for s in board["shots"]) / 24, 3)
        # total_seconds 里含沿用段，但没写 inherited_seconds → 必须报"不一致"
        board["total_seconds"] = round(shots_seconds + self.inherited, 3)
        problems = make_storyboard.storyboard_problems(
            board, self.conf, float(board["total_seconds"]), None)
        self.assertTrue(any("不一致" in p for p in problems), problems)

    def test_screenplay_invariant_accepts_inherited(self) -> None:
        artifact = {
            "total_seconds": 120.0,
            "inherited_seconds": self.inherited,
            "scenes": [{"estimated_seconds": 40.0}, {"estimated_seconds": 49.63}],
        }
        self.assertEqual(make_story.duration_problems(artifact, 120.0, self.conf), [])
        # 少了 inherited_seconds，就要报"不一致"
        artifact.pop("inherited_seconds")
        problems = make_story.duration_problems(artifact, 120.0, self.conf)
        self.assertTrue(any("不一致" in p for p in problems), problems)


class SupersededTermsTest(unittest.TestCase):
    """按片配置的"旧说法"禁用表：续篇里上一部片的旧词不该误伤本片。"""

    def test_bible_leak_guard_blocks_stage_plan_in_character_table(self) -> None:
        """角色表里不许写"整片演进计划／账本总计数"——它会经人物锁定行泄漏进每一镜。

        2026-10-15 实测事故：锁定行写了"阶段一耳廓立起…阶段三完整的动物头"与"别针 0→1→2→3"，
        模型在第一镜就画出了兽耳与多余的别针，整段作废重渲。
        """
        clean = {"characters": [{"id": "CH01", "appearance": "中年男性，同一张脸跨镜一致",
                                 "wardrobe": "灰色长袖衬衫＋深色围巾"}]}
        self.assertEqual([], make_prompts.character_bible_leak_problems(clean))
        leaky = {"characters": [{"id": "CH01", "appearance": "本片分三阶段渐进动物化：阶段一耳廓立起、阶段三完整的动物头",
                                 "wardrobe": "内衬别针 0→1→2→3，第三笔当场入账"}]}
        hits = make_prompts.character_bible_leak_problems(leaky)
        self.assertTrue(hits)
        self.assertIn("CH01", hits[0])
        # 本项目里真实存在的角色表必须是干净的
        real = json.loads((PROJECT_ROOT / "projects" / "animal-heads-nap-02" / "screenplay.json")
                          .read_text(encoding="utf-8"))["continuity_bible"]
        self.assertEqual([], make_prompts.character_bible_leak_problems(real))

    def test_prune_unknown_drops_only_extra_keys(self) -> None:
        """多写的字段由程序按 schema 剔除（并记账），不再作废整轮生成。"""
        schema = {
            "type": "object",
            "properties": {
                "shots": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"shot_id": {"type": "string"},
                                   "state": {"$ref": "#/$defs/state"}}}},
                "total_seconds": {"type": "number"},
            },
            "additionalProperties": False,
            "$defs": {"state": {"type": "object", "additionalProperties": False,
                                "properties": {"gaze": {"type": "string"}}}},
        }
        data = {
            "shots": [{"shot_id": "SH01", "risk_facts": ["x"], "state": {"gaze": "看杯子", "extra": 1}}],
            "total_seconds": 120.0,
            "space_plan_note": "多余",
        }
        dropped = make_ideas.prune_unknown(data, schema)
        self.assertEqual([], make_ideas.schema_errors(data, schema))
        self.assertEqual({"shots", "total_seconds"}, set(data))
        self.assertEqual({"shot_id", "state"}, set(data["shots"][0]))
        self.assertEqual({"gaze"}, set(data["shots"][0]["state"]))
        self.assertIn("space_plan_note", dropped)
        self.assertIn("shots.0.risk_facts", dropped)
        self.assertIn("shots.0.state.extra", dropped)

    def test_tolerant_json_fixes_handle_stray_quote_and_trailing_comma(self) -> None:
        """多一个引号、多一个尾逗号这两类笔误要能被容错救回来。"""
        stray = make_ideas.extract_json('{"a": 1, "b": [1, 2]""}')
        self.assertEqual({"a": 1, "b": [1, 2]}, stray)
        trailing = make_ideas.extract_json('{"a": [1, 2,], "b": 1,}')
        self.assertEqual({"a": [1, 2], "b": 1}, trailing)

    def test_nap02_uses_its_own_list(self) -> None:
        terms = make_storyboard.superseded_terms("animal-heads-nap-02")
        self.assertIn("人皮面具", terms)
        self.assertNotIn("镜面反光", terms)

    def test_unknown_film_falls_back_to_default(self) -> None:
        terms = make_storyboard.superseded_terms("no-such-film")
        self.assertEqual(terms, make_storyboard.DEFAULT_SUPERSEDED_TERMS)


if __name__ == "__main__":
    unittest.main()
