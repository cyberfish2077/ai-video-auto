"""快速出片通道（第三条路线）的自动化测试：**不调模型、不花显卡**。

守的是 2026-10-15 首次真跑暴露出来的四类问题：

1. **提示词里 schema 全文 dump**（11k 字里近 9k 是 `description`）→ 模型在尾部结构上翻车
   （少一个闭合括号 / 多写一个 `}`）。现在换成"只有字段名/类型/必填"的骨架，机械校验仍用完整 schema。
2. **骨架删掉了藏在 `description` 里的硬性规则**：`prompt` 一律 null、`total_seconds` 必须精确等于
   各镜帧数之和、素材 `aspect_ratio` 至少 4 个字符。这些规则现在由共用常量显式写进提示词。
3. **返修轮数上限**：`--repairs` 默认 2 → 每段最多 3 次调用，超了就报错停下（不无限重试）。
4. **与严格版隔离**：只写 `*_fast` 产物与 `prompts_fast/`，**不碰**严格版的
   `screenplay.json / storyboard.json / prompt_pack.json / state.json / prompts/`。

端到端那几条用一个"假 DeepSeek 客户端"喂**已落盘的真实产物**当回复（同样是零花费），
并把项目目录指到临时目录，跑完不留痕。

运行方式（在项目根目录）::

    .venv\\Scripts\\python.exe -m unittest discover -s director_system/tests -t director_system/tests
"""

from __future__ import annotations

import io
import json
import os
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
from video_auto import materials as materials_mod  # noqa: E402
from video_auto import tasks as tasks_mod  # noqa: E402

import make_film_fast as fast  # noqa: E402
import make_ideas  # noqa: E402
import make_prompts  # noqa: E402
import make_storyboard  # noqa: E402

#: 用哪部真片子的产物当"模型回复"的夹具（零花费：不调模型，只重放已落盘的结果）
FIXTURE_FILM = "_fast_smoke"
TEST_FILM = "fast-isolation-test"


def load_fixture(name: str) -> dict | None:
    path = cfg.load_settings().path("paths.projects") / FIXTURE_FILM / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def unused_asset_ids(pack: dict) -> list[str]:
    """列出"没人用"的资产 id（镜头没引用、也没被别的资产派生引用）。"""
    used = {str(img.get("asset_id")) for shot in pack.get("shots") or []
            for img in shot.get("reference_images") or []}
    used |= {src.strip() for asset in pack.get("assets") or []
             for src in str(asset.get("derived_from") or "").split(",") if src.strip()}
    return [str(a.get("asset_id")) for a in pack.get("assets") or []
            if str(a.get("asset_id")) not in used]


def drop_unused_assets(pack: dict, board: dict | None = None) -> tuple[dict, dict | None]:
    """夹具用：把"没人用"的资产从提示词包（以及分镜的资产表）里去掉。

    真实夹具（`_fast_smoke` 的 prompt_pack_fast.json）是在**新增这条规则之前**生成的，
    里面留着一条 `ast_canvas_bag`（出了图没有镜头引用）。新规则会（正确地）拒绝它，
    所以喂给假客户端的回复要先按新规则清理干净——这正是"新生成应该长什么样"。

    分镜那边要一起改：提示词包的机械校验要求"分镜资产表里的每一条都有出图提示词"，
    只删一边会从另一个方向报错。
    """
    unused = set(unused_asset_ids(pack))
    cleaned = dict(pack)
    cleaned["assets"] = [a for a in pack.get("assets") or []
                         if str(a.get("asset_id")) not in unused]
    if board is None:
        return cleaned, None
    board_clean = dict(board)
    board_clean["asset_registry"] = [a for a in board.get("asset_registry") or []
                                     if str(a.get("asset_id")) not in unused]
    return cleaned, board_clean


class _StubClient:
    """假 DeepSeek 客户端：按顺序把夹具当回复吐出来，并记账调用次数。"""

    def __init__(self, replies: list[str], settings=None, film_id: str = TEST_FILM):
        self._replies = list(replies)
        self.settings = settings
        self.model = "stub"
        self.last_finish_reason = "stop"
        self.last_usage: dict = {}
        self.calls: list[str] = []
        self._film_id = film_id

    def chat(self, messages, *, json_mode: bool = True, temperature: float = 0.0) -> str:
        self.calls.append(str(messages[0].get("content", "")))
        if not self._replies:
            return "{}"
        raw = self._replies.pop(0)
        try:
            data = json.loads(raw)
            data["film_id"] = self._film_id
            raw = json.dumps(data, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        return raw


def make_settings(projects_root: Path) -> cfg.Settings:
    """把真实配置里的 `paths.projects` 指到临时目录，其余原样。"""
    real = cfg.load_settings()
    raw = json.loads(json.dumps(real.raw))
    raw.setdefault("paths", {})["projects"] = str(projects_root)
    return cfg.Settings(raw=raw, source=real.source)


def run_fast(film_id: str, brief_path: Path, extra: list[str] | None = None) -> tuple[int, str]:
    argv = ["make_film_fast.py", "--film-id", film_id, "--brief-file", str(brief_path)]
    argv += extra or []
    buffer = io.StringIO()
    with mock.patch.object(sys, "argv", argv), redirect_stdout(buffer):
        code = fast.main()
    return code, buffer.getvalue()


class SchemaSummaryTest(unittest.TestCase):
    """字段骨架：够短、够准、足够让模型把结构写对。"""

    NAMES = ("screenplay.schema.json", "storyboard.schema.json", "prompt_pack.schema.json")

    def test_summary_is_valid_json_and_much_shorter(self) -> None:
        for name in self.NAMES:
            schema = make_ideas.load_schema(name)
            summary = make_ideas.schema_summary(schema)
            json.loads(summary)  # 必须是合法 JSON（模型看到的是同一个形状）
            full = json.dumps(schema, ensure_ascii=False, indent=2)
            self.assertLess(len(summary), len(full) * 0.45,
                            f"{name} 的骨架没压下来：{len(summary)} vs {len(full)}")

    def test_marks_required_and_array_size(self) -> None:
        summary = make_ideas.schema_summary(make_ideas.load_schema("screenplay.schema.json"))
        self.assertIn('"film_id*"', summary, "必填字段的键名要带 *")
        self.assertIn('"scenes*[2~6 项]"', summary, "数组要标出元素个数要求")
        self.assertNotIn('"equal_length_justification*"', summary, "可选字段不该被标成必填")

    def test_shows_length_limits_but_not_descriptions(self) -> None:
        summary = make_ideas.schema_summary(make_ideas.load_schema("prompt_pack.schema.json"))
        # aspect_ratio 是 minLength: 4 —— 2026-10-15 实测：模型写了 "3:4" 被机械校验判死
        self.assertIn("至少 4 个字符", summary)
        # 说明文字（description）不该再出现在骨架里
        self.assertNotIn("阶段 4 一律为 null", summary)

    def test_legend_explains_the_notation(self) -> None:
        legend = make_ideas.SCHEMA_SUMMARY_LEGEND
        for token in ("`*`", "[n~m 项]", "不要照抄"):
            self.assertIn(token, legend)


class FastPromptTest(unittest.TestCase):
    """三段提示词：用骨架、把硬性规则说回来、结尾带结构自查。"""

    def setUp(self) -> None:
        self.conf = cfg.load_director_config()
        self.schemas = {name: make_ideas.load_schema(name + ".schema.json")
                        for name in ("screenplay", "storyboard", "prompt_pack")}
        self.brief = load_fixture("brief.json")
        self.screenplay = load_fixture("screenplay_fast.json")
        self.board = load_fixture("storyboard_fast.json")
        if not (self.brief and self.screenplay and self.board):
            self.skipTest(f"需要 {FIXTURE_FILM} 的已生成产物做夹具（先跑一次 make_film_fast.py）")

    def test_screenplay_prompt_uses_skeleton(self) -> None:
        text = fast.screenplay_prompt(self.brief, self.conf, self.schemas["screenplay"],
                                      "photoreal-cinematic")
        self.assertIn("字段骨架", text)
        self.assertLess(len(text), 8000, "剧本提示词不该再被 schema 说明撑到上万字")
        # 结构自查（括号闭合）由 make_ideas.FORMAT_RULES 随每次请求追加，三条路线共用一份
        self.assertIn("结构必须闭合", make_ideas.FORMAT_RULES)
        self.assertIn("最后一个元素后面不要再写逗号", make_ideas.FORMAT_RULES)
        self.assertEqual(0, fast.screenplay_prompt(self.brief, self.conf, self.schemas["screenplay"],
                                                  "photoreal-cinematic").count("输出前自查"),
                         "快速通道不该再自己追加一遍（会和共用规则重复）")

    def test_storyboard_prompt_states_the_rules_that_were_in_descriptions(self) -> None:
        text = fast.storyboard_prompt(self.brief, self.screenplay, self.conf,
                                      self.schemas["storyboard"])
        self.assertIn(make_storyboard.BOARD_NO_PROMPT_RULE, text)
        self.assertIn("`prompt` 必须写 `null`", text)
        self.assertIn("精确等于", text, "total_seconds 必须等于各镜帧数之和（机械校验的硬口径）")

    def test_pack_prompt_states_asset_rules(self) -> None:
        text = fast.pack_prompt(self.brief, self.board, "", "photoreal-cinematic",
                                self.conf, self.schemas["prompt_pack"])
        self.assertIn("至少 4 个字符", text)
        self.assertIn("字段骨架", text)

    def test_strict_route_keeps_the_full_schema(self) -> None:
        """严格版**没有**走精简路径：它的提示词里仍然是完整 schema（含 description）。"""
        text = make_prompts.compile_user_prompt(
            self.brief, self.board, {}, "", "示例风格前缀", "photoreal-cinematic",
            self.conf, self.schemas["prompt_pack"])
        self.assertIn("严格按下面的 JSON Schema 输出：", text)
        self.assertIn('"description"', text)
        self.assertNotIn("字段骨架", text)


class RepairsCapTest(unittest.TestCase):
    """返修有上限：`--repairs 2` 意味着最多 3 次调用，超了就停下报错。"""

    def test_call_json_gives_up_after_the_cap(self) -> None:
        client = _StubClient(["这不是 JSON", "这也不是", "还不是"])
        with self.assertRaises(RuntimeError) as caught:
            make_ideas.call_json(client, system="s", user="u",
                                 schema=make_ideas.load_schema("idea_review.schema.json"),
                                 temperature=0.0, repairs=2, label="测试")
        self.assertEqual(len(client.calls), 3, "repairs=2 → 首次 + 2 轮修复，一共 3 次")
        self.assertIn("连续 3 次", str(caught.exception))

    def test_default_repairs_is_two(self) -> None:
        """默认值写在命令行参数里：读源码确认它是 2（不是 1）。"""
        import inspect
        source = inspect.getsource(fast.main)
        self.assertIn('"--repairs", type=int, default=2', source)


class FastRouteEndToEndTest(unittest.TestCase):
    """整条流程（剧本→分镜→提示词→渲染计划→出图批次）用夹具跑通，不碰真实项目目录。"""

    def setUp(self) -> None:
        raw = [load_fixture(name) for name in
               ("screenplay_fast.json", "storyboard_fast.json", "prompt_pack_fast.json")]
        if not all(raw):
            self.skipTest(f"需要 {FIXTURE_FILM} 的已生成产物做夹具")
        pack, board = drop_unused_assets(raw[2], raw[1])
        self.fixtures = [raw[0], board, pack]
        brief = load_fixture("brief.json")
        if not brief:
            self.skipTest(f"需要 {FIXTURE_FILM}/brief.json 做夹具")
        self._tmp = tempfile.TemporaryDirectory(prefix="video-auto-fast-")
        self.addCleanup(self._tmp.cleanup)
        self.projects_root = Path(self._tmp.name)
        self.film_dir = self.projects_root / TEST_FILM
        self.film_dir.mkdir(parents=True)
        self.brief_path = self.film_dir / "brief.json"
        self.brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        self.settings = make_settings(self.projects_root)
        for target in (cfg, fast.cfg):
            patch = mock.patch.object(target, "load_settings", lambda: self.settings)
            patch.start()
            self.addCleanup(patch.stop)
        self.client = _StubClient(
            [json.dumps(x, ensure_ascii=False) for x in self.fixtures], self.settings)
        patch_client = mock.patch.object(fast.ds, "DeepSeekClient", lambda settings: self.client)
        patch_client.start()
        self.addCleanup(patch_client.stop)

    def test_writes_only_fast_artifacts_and_leaves_strict_ones_alone(self) -> None:
        # 先放一份"严格版产物"，跑完必须原封不动
        strict_marker = {"film_id": TEST_FILM, "不要动我": True}
        strict_files = {
            "screenplay.json": strict_marker,
            "storyboard.json": strict_marker,
            "prompt_pack.json": strict_marker,
            "state.json": strict_marker,
        }
        for name, payload in strict_files.items():
            (self.film_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        code, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(code, 0, output)

        for name in ("screenplay_fast.json", "storyboard_fast.json", "prompt_pack_fast.json",
                     "render_plan_fast.json", "state_fast.json",
                     "materials_definition_fast.json"):
            self.assertTrue((self.film_dir / name).is_file(), f"缺少快速通道产物 {name}")
        self.assertTrue((self.film_dir / "prompts_fast").is_dir())
        self.assertFalse((self.film_dir / "prompts").exists(), "快速通道不该写严格版的 prompts/")
        self.assertFalse((self.film_dir / "history").exists(), "首跑不该产生归档目录")
        for name, payload in strict_files.items():
            kept = json.loads((self.film_dir / name).read_text(encoding="utf-8"))
            self.assertEqual(kept, payload, f"严格版产物被改动了：{name}")

        state = json.loads((self.film_dir / "state_fast.json").read_text(encoding="utf-8"))
        self.assertEqual(state["route"], "fast")
        self.assertNotIn("gates", state, "快速通道不写闸门")

    def test_program_lines_and_render_plan_are_consistent(self) -> None:
        code, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(code, 0, output)
        board = json.loads((self.film_dir / "storyboard_fast.json").read_text(encoding="utf-8"))
        pack = json.loads((self.film_dir / "prompt_pack_fast.json").read_text(encoding="utf-8"))
        plan = json.loads((self.film_dir / "render_plan_fast.json").read_text(encoding="utf-8"))
        self.assertEqual(len(plan["segments"]), len(board["shots"]))
        for shot in pack["shots"]:
            self.assertTrue(shot["prompt_lines"]["ref_declaration"], "参考图声明行由程序派生，不能空")
            self.assertTrue(shot["prompt_lines"]["character_lock"], "人物锁定行由程序派生，不能空")
        for segment in plan["segments"]:
            prompt_file = PROJECT_ROOT / segment["prompt_file"]
            self.assertTrue(prompt_file.is_file() or TEST_FILM in str(prompt_file),
                            f"渲染计划指向的提示词文件不存在：{prompt_file}")

    def test_asset_definitions_split_by_target(self) -> None:
        code, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(code, 0, output)
        pack = json.loads((self.film_dir / "prompt_pack_fast.json").read_text(encoding="utf-8"))
        definitions = fast.build_asset_definitions(TEST_FILM, pack)
        self.assertEqual({data["target"] for _, data, _ in definitions},
                         {str(a["qwen_target"]) for a in pack["assets"]})
        for name, data, _ in definitions:
            self.assertTrue(data["name"].startswith(f"{TEST_FILM}-fast-"),
                            f"{name} 的项目名必须带 -fast- 前缀，避免和严格版撞名")
            for segment in data["segments"]:
                self.assertTrue(segment["prompt"], "出图提示词不能空")
                if data["target"] != fast.MATERIALS_TARGET:
                    self.assertTrue(segment.get("images"), "派生批必须写明来源图（ref_image_N 顺序）")

    def test_second_run_reuses_validated_stages_and_spends_no_calls(self) -> None:
        first, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(first, 0, output)
        self.assertEqual(len(self.client.calls), 3, "首次跑三段各一次调用")
        code, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.client.calls), 3, "第二轮应当全部复用，不再花调用")
        self.assertIn("复用已有", output)

    def test_repairs_cap_stops_the_run_instead_of_looping(self) -> None:
        self.client._replies = ["坏 JSON", "还是坏", "仍然是坏的"]
        code, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.client.calls), 3, "repairs=2 → 最多 3 次就该停")
        self.assertIn("[错误] 剧本生成失败", output)
        self.assertIn("下一步", output, "报错必须带「下一步怎么办」")

    def test_dry_run_writes_nothing(self) -> None:
        code, output = run_fast(TEST_FILM, self.brief_path)
        self.assertEqual(code, 0, output)
        before = {str(p): p.stat().st_mtime_ns for p in self.film_dir.rglob("*") if p.is_file()}
        calls_before = len(self.client.calls)
        code, output = run_fast(TEST_FILM, self.brief_path, ["--dry-run"])
        self.assertEqual(code, 0, output)
        after = {str(p): p.stat().st_mtime_ns for p in self.film_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "dry-run 不该改动任何文件")
        self.assertEqual(len(self.client.calls), calls_before, "dry-run 不该调模型")
        self.assertIn("没有调用模型、没有写任何文件", output)


class UnusedAssetRuleTest(unittest.TestCase):
    """规则：资产必须被某个镜头用到（或被别的资产派生引用），否则白出一次图。"""

    def setUp(self) -> None:
        self.pack = load_fixture("prompt_pack_fast.json")
        if not self.pack:
            self.skipTest(f"需要 {FIXTURE_FILM} 的提示词包做夹具")

    def test_flags_assets_nobody_uses(self) -> None:
        problems = make_prompts.unused_asset_problems(self.pack)
        self.assertTrue(problems, "夹具里就有一条没人用的资产，必须报出来")
        self.assertIn("ast_canvas_bag", problems[0])
        cleaned, _ = drop_unused_assets(self.pack)
        self.assertEqual([], make_prompts.unused_asset_problems(cleaned), "清理之后就不该再有提示")

    def test_derived_source_counts_as_used(self) -> None:
        pack = {
            "assets": [
                {"asset_id": "A", "qwen_target": "qwen_t2i"},
                {"asset_id": "B", "qwen_target": "qwen_multiref", "derived_from": "A"},
            ],
            "shots": [{"reference_images": [{"asset_id": "B"}]}],
        }
        self.assertEqual([], make_prompts.unused_asset_problems(pack),
                         "A 只作为派生来源也算用到了（正面→侧面这种链）")

    def test_fast_route_blocks_new_output_but_tolerates_old_artifacts(self) -> None:
        """新生成必须过这条规则；已经出完成片的旧产物不该因为新规则被判死。"""
        board = load_fixture("storyboard_fast.json")
        screenplay = load_fixture("screenplay_fast.json")
        if not (board and screenplay):
            self.skipTest("需要分镜/剧本夹具")
        conf = cfg.load_director_config()
        schema = make_ideas.load_schema("prompt_pack.schema.json")
        bible = screenplay.get("continuity_bible") or {}
        blocked = fast.pack_problems(self.pack, board, bible, conf, "photoreal-cinematic", schema)
        self.assertTrue(any("ast_canvas_bag" in item for item in blocked), "新生成必须拦住它")
        tolerated = fast.pack_problems(self.pack, board, bible, conf, "photoreal-cinematic", schema,
                                       check_unused_assets=False)
        self.assertFalse(any("ast_canvas_bag" in item for item in tolerated),
                         "复用旧产物时不该因为这条新规则被判死（其余硬问题仍照查）")


class AutoSyncReferenceImagesTest(unittest.TestCase):
    """提交前自动补参考图：多图参考批次"后一级要读同一批刚出的图"不再需要手工同步。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="video-auto-sync-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.projects_root = root / "projects"
        self.input_dir = root / "comfyui-input"
        self.film_dir = self.projects_root / "some-film"
        self.materials_dir = self.projects_root / "some-film-fast-boards" / "materials"
        for path in (self.film_dir, self.materials_dir, self.input_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.runner = tasks_mod.TaskRunner(
            client=None, ui=None, workflow_path=Path("w.json"), target_cfg={}, store=None,
            index=None, output_dir=root / "out", archive_root=root / "arch",
            input_dir=self.input_dir, projects_root=self.projects_root)
        self.seg = tasks_mod.Segment(id="s1", prompt="p", images=["ast_a.png"])
        self.project = tasks_mod.Project(name="some-film", target="qwen_multiref")

    def test_copies_missing_image_from_material_dirs(self) -> None:
        source = self.materials_dir / "ast_a_00001_.png"
        source.write_bytes(b"png")
        self.runner._sync_reference_images(
            self.project, self.seg, {"ref_image_1": "ast_a.png"})
        self.assertTrue((self.input_dir / "ast_a.png").exists(), "提交前应当自动补进 input")

    def test_refreshes_stale_copy_but_keeps_chain_anchors(self) -> None:
        stale = self.input_dir / "ast_a.png"
        stale.write_bytes(b"old")
        os.utime(stale, (1, 1))
        source = self.materials_dir / "ast_a_00001_.png"
        source.write_bytes(b"new")
        self.runner._sync_reference_images(
            self.project, self.seg, {"ref_image_1": "ast_a.png"})
        self.assertEqual(b"new", stale.read_bytes(), "素材更新时应当刷新 input 里的那份")

        anchor = self.input_dir / "chain-s0.png"
        anchor.write_bytes(b"anchor")
        self.runner._sync_reference_images(
            self.project, self.seg, {"ref_image_1": "ast_a.png", "ref_image_2": "chain-s0.png"})
        self.assertEqual(b"anchor", anchor.read_bytes(), "连续性锚点不在素材目录里，不能被当成缺失")

    def test_warns_early_when_image_is_not_generated_yet(self) -> None:
        with self.assertLogs(self.runner.log, level="WARNING") as caught:
            self.runner._sync_reference_images(
                self.project, self.seg, {"ref_image_1": "not_yet.png"})
        self.assertIn("not_yet.png", "\n".join(caught.output))

    def test_can_be_turned_off(self) -> None:
        patch = mock.patch.object(self.runner, "auto_sync_images", False)
        patch.start()
        self.addCleanup(patch.stop)
        (self.materials_dir / "ast_a_00001_.png").write_bytes(b"png")
        self.runner._sync_reference_images(self.project, self.seg, {"ref_image_1": "ast_a.png"})
        self.assertFalse((self.input_dir / "ast_a.png").exists(), "--no-auto-sync 时不该动 input")


class MaterialLookupTest(unittest.TestCase):
    """查找规则：脚本（手工同步）与任务管理器（自动同步）用的是同一份。"""

    def test_fast_batch_dirs_are_known(self) -> None:
        source = (PROJECT_ROOT / "src" / "video_auto" / "materials.py").read_text(encoding="utf-8")
        for name in ("-fast-materials", "-fast-derive", "-fast-boards"):
            self.assertIn(f'"{name}"', source, f"查找规则不认 {name} 目录")

    def test_priority_is_by_directory_then_newest(self) -> None:
        self.assertEqual("some-film", materials_mod.base_project_name("some-film-fast-boards"))
        self.assertEqual("some-film", materials_mod.base_project_name("some-film-materials"))
        self.assertEqual("some-film", materials_mod.base_project_name("some-film"))
        with tempfile.TemporaryDirectory(prefix="video-auto-mat-") as tmp:
            first, second = Path(tmp) / "a", Path(tmp) / "b"
            first.mkdir()
            second.mkdir()
            (first / "x.png").write_bytes(b"1")
            (second / "x_00001_.png").write_bytes(b"2")
            (first / "y_00001_.png").write_bytes(b"3")
            (first / "y_00002_.png").write_bytes(b"4")
            os.utime(first / "y_00001_.png", (1, 1))   # 明确时间：00001 更旧
            self.assertEqual(first / "x.png", materials_mod.find_material([first, second], "x.png"))
            self.assertEqual(first / "y_00002_.png",
                             materials_mod.find_material([first, second], "y.png"),
                             "同一目录内取计数最大的那张（重跑会往后加计数）")
            self.assertIsNone(materials_mod.find_material([first, second], "zzz.png"))


if __name__ == "__main__":
    unittest.main()
