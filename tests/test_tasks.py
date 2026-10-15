"""任务管理器的单元测试（阶段六）。

用"假 ComfyUI 客户端"在本地验证重试、续跑、去重与台账落盘，
不需要显卡、不需要真的启动 ComfyUI。

运行::

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import comfy, generate, tasks  # noqa: E402


def make_record(prompt_id: str, filename: str, subfolder: str = "video_auto") -> dict:
    return {
        "prompt_id": prompt_id,
        "status": {"completed": True, "status_str": "success"},
        "outputs": {"137": {"gifs": [{"filename": filename, "subfolder": subfolder,
                                      "type": "output"}]}},
    }


class FakeClient:
    """可编排成功/失败的假客户端，接口与 ComfyClient 一致。"""

    def __init__(self, *, fail_first: int = 0, always_fail: bool = False,
                 history_records: dict | None = None) -> None:
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.submits = 0
        self.records: dict[str, dict] = {}
        self.history_records = history_records or {}

    def queue_state(self) -> tuple[int, int]:
        return (0, 0)

    def submit(self, prompt: dict, extra_data: dict | None = None) -> str:
        self.submits += 1
        if self.always_fail or self.submits <= self.fail_first:
            raise comfy.ComfyError("模拟提交失败（用于测试重试）")
        prompt_id = f"pid-{self.submits}"
        self.records[prompt_id] = make_record(prompt_id, "seg-01_00001.mp4")
        return prompt_id

    def history(self, prompt_id: str) -> dict | None:
        return self.history_records.get(prompt_id)

    def wait(self, prompt_id: str, *, timeout_seconds: int, poll_seconds: float = 5.0,
             on_tick=None) -> dict:
        if on_tick:
            on_tick(0, (0, 0))
        if prompt_id not in self.records:
            raise comfy.ComfyError(f"模拟等待超时：{prompt_id}")
        return self.records[prompt_id]

    def interrupt(self) -> None:
        pass


class UiStub:
    """假的 UI→API 转换器，只要能返回一个合法 prompt 字典即可。"""

    def __init__(self) -> None:
        self.last_bypass: set[int] = set()

    def to_api_prompt(self, overrides=None, bypass_nodes=None) -> dict:
        self.last_bypass = set(bypass_nodes or set())
        return {"1": {"class_type": "Stub", "inputs": dict(overrides or {})}}


class TaskRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workflow_file = self.root / "wf.json"
        self.workflow_file.write_text('{"nodes": [], "links": []}', encoding="utf-8")
        self.output_dir = self.root / "comfy_output"
        self.archive_root = self.root / "project_segments"
        self.store = tasks.ProjectStore(self.root / "data/projects/batch-test")
        self.index = tasks.GenerationIndex(self.root / "cache/generation_index.json")
        self.log = logging.getLogger("test.tasks")
        self.log.addHandler(logging.NullHandler())
        # 假 ComfyUI 的输出文件必须真实存在，归档与校验才有意义
        out_file = self.output_dir / "video_auto" / "seg-01_00001.mp4"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"fake-video-bytes")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_project(self, **segment_kwargs) -> tasks.Project:
        segment = tasks.Segment(id="seg-01", prompt="测试提示词", seed=42,
                                duration_seconds=5, width=960, height=544, **segment_kwargs)
        project = tasks.Project(name="batch-test", target="t2v_test",
                                defaults={"duration_seconds": 5, "width": 960, "height": 544},
                                segments=[segment])
        self.store.save(project)
        return project

    def make_runner(self, client) -> tasks.TaskRunner:
        return tasks.TaskRunner(
            client=client,
            ui=UiStub(),
            workflow_path=self.workflow_file,
            target_cfg={"overrides": {"prompt": {"node": 1, "input": "value"}}},
            store=self.store,
            index=self.index,
            output_dir=self.output_dir,
            archive_root=self.archive_root,
            ffprobe=None,
            logger=self.log,
        )

    def fingerprint_for(self, values: dict) -> str:
        """按任务管理器的方式算指纹（含"登记表实际改写到的节点映射"）。"""
        target_cfg = {"overrides": {"prompt": {"node": 1, "input": "value"}}}
        return generate.segment_fingerprint(
            self.workflow_file, values,
            overrides=generate.build_overrides(target_cfg, values))

    def test_registry_mapping_changes_fingerprint(self) -> None:
        """登记表改写点变了（例如键名从错的 text 改回 prompt），指纹必须跟着变。

        2026-10-13 实测踩过：TTS 登记表键名写错 → 台词没进模型 → 产物是示例台词。
        若指纹不含映射，修好登记表后会因为"指纹命中"而复用回那份错产物。
        """
        values = {"target": "t2v_test", "prompt": "同一段提示词", "seed": 7}
        wrong = generate.build_overrides(
            {"overrides": {"text": {"node": 38, "input": "text"}}}, values)
        right = generate.build_overrides(
            {"overrides": {"prompt": {"node": 38, "input": "text"}}}, values)
        self.assertNotEqual(
            generate.segment_fingerprint(self.workflow_file, values, overrides=wrong),
            generate.segment_fingerprint(self.workflow_file, values, overrides=right))

    # ---------------- 正常成功 ----------------

    def test_success_marks_done_archives_and_indexes(self) -> None:
        project = self.make_project()
        runner = self.make_runner(FakeClient())
        counts = runner.run(project, max_attempts=3, retry_backoff=0.0, timeout_seconds=10)

        self.assertEqual(counts.get(tasks.STATUS_DONE), 1)
        segment = self.store.load().segments[0]
        self.assertEqual(segment.status, tasks.STATUS_DONE)
        self.assertEqual(segment.attempts, 1)
        self.assertTrue(segment.output_path)
        self.assertTrue(Path(segment.output_path).exists(), "产物应已归档到项目目录")
        self.assertTrue(Path(segment.output_path).is_relative_to(self.archive_root))
        # 成功结果应写进去重索引
        self.assertIsNotNone(self.index.get(segment.fingerprint))

    # ---------------- 失败重试 ----------------

    def test_retry_then_success(self) -> None:
        project = self.make_project()
        client = FakeClient(fail_first=2)      # 前两次失败，第三次成功
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=3, retry_backoff=0.0, timeout_seconds=10)

        self.assertEqual(counts.get(tasks.STATUS_DONE), 1)
        segment = self.store.load().segments[0]
        self.assertEqual(segment.attempts, 3)
        self.assertEqual(client.submits, 3)
        self.assertIsNone(segment.error)
        events = [h["event"] for h in segment.history]
        self.assertEqual(events.count("attempt_failed"), 2)
        self.assertIn("attempt_ok", events)

    def test_gives_up_after_max_attempts(self) -> None:
        project = self.make_project()
        client = FakeClient(always_fail=True)
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=3, retry_backoff=0.0, timeout_seconds=10)

        self.assertEqual(counts.get(tasks.STATUS_FAILED), 1)
        segment = self.store.load().segments[0]
        self.assertEqual(segment.status, tasks.STATUS_FAILED)
        self.assertEqual(segment.attempts, 3)
        self.assertIn("模拟提交失败", segment.error or "")

    def test_stop_on_error_stops_remaining_segments(self) -> None:
        project = self.make_project()
        project.segments.append(tasks.Segment(id="seg-02", prompt="第二段", seed=43))
        self.store.save(project)
        runner = self.make_runner(FakeClient(always_fail=True))
        runner.run(project, max_attempts=1, retry_backoff=0.0, timeout_seconds=10,
                   stop_on_error=True)
        reloaded = self.store.load()
        self.assertEqual(reloaded.segments[0].status, tasks.STATUS_FAILED)
        self.assertEqual(reloaded.segments[1].status, tasks.STATUS_PENDING, "第二个片段不应被执行")

    # ---------------- 去重 ----------------

    def test_only_filter_keeps_other_segments_in_ledger(self) -> None:
        """only= 只影响"这次跑哪些"，绝不能把台账里别的片段抹掉。

        2026-10-13 实测踩过：run --id 过滤后项目被存回台账，
        72 段的台账只剩 7 段、57 段的只剩 6 段，且不可逆。
        """
        project = self.make_project()
        project.segments.append(tasks.Segment(id="seg-02", prompt="第二段", seed=43))
        self.store.save(project)
        runner = self.make_runner(FakeClient())
        runner.run(project, max_attempts=1, retry_backoff=0.0, timeout_seconds=10,
                   only=["seg-02"])
        reloaded = self.store.load()
        self.assertEqual([seg.id for seg in reloaded.segments], ["seg-01", "seg-02"],
                         "台账被 only 过滤改写了")
        self.assertEqual(reloaded.segments[0].status, tasks.STATUS_PENDING,
                         "没被选中的片段应保持原状")
        self.assertEqual(reloaded.segments[1].status, tasks.STATUS_DONE)

    def test_reuse_skips_generation(self) -> None:
        project = self.make_project()
        segment = project.segments[0]
        values = {"target": "t2v_test", "prompt": segment.prompt, "duration_seconds": 5,
                  "width": 960, "height": 544, "seed": 42}
        fingerprint = self.fingerprint_for(values)
        existing = self.archive_root / "old_result.mp4"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"old")
        self.index.put(fingerprint, {"prompt_id": "old-pid", "path": str(existing)})

        client = FakeClient()
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=3, retry_backoff=0.0, timeout_seconds=10)

        self.assertEqual(counts.get(tasks.STATUS_REUSED), 1)
        self.assertEqual(client.submits, 0, "命中去重时不应提交任何任务")
        self.assertEqual(self.store.load().segments[0].output_path, str(existing))

    def test_no_reuse_flag_forces_regeneration(self) -> None:
        project = self.make_project()
        segment = project.segments[0]
        values = {"target": "t2v_test", "prompt": segment.prompt, "duration_seconds": 5,
                  "width": 960, "height": 544, "seed": 42}
        fingerprint = self.fingerprint_for(values)
        existing = self.archive_root / "old_result.mp4"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"old")
        self.index.put(fingerprint, {"prompt_id": "old-pid", "path": str(existing)})

        client = FakeClient()
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=1, retry_backoff=0.0, timeout_seconds=10,
                            reuse_existing=False)
        self.assertEqual(counts.get(tasks.STATUS_DONE), 1)
        self.assertEqual(client.submits, 1)

    # ---------------- 断点续跑 ----------------

    def test_resume_adopts_finished_job_without_resubmitting(self) -> None:
        project = self.make_project(prompt_id="pid-resumed")
        client = FakeClient(history_records={"pid-resumed": make_record("pid-resumed", "seg-01_00001.mp4")})
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=3, retry_backoff=0.0, timeout_seconds=10)

        self.assertEqual(counts.get(tasks.STATUS_DONE), 1)
        self.assertEqual(client.submits, 0, "续跑收编不应重新提交")
        segment = self.store.load().segments[0]
        self.assertEqual(segment.status, tasks.STATUS_DONE)
        self.assertIn("resume_adopt", [h["event"] for h in segment.history])
        self.assertTrue(Path(segment.output_path or "").exists())

    def test_resume_resubmits_when_previous_job_unfinished(self) -> None:
        project = self.make_project(prompt_id="pid-gone")
        client = FakeClient()      # 历史里查不到该 prompt_id
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=2, retry_backoff=0.0, timeout_seconds=10)

        self.assertEqual(counts.get(tasks.STATUS_DONE), 1)
        self.assertEqual(client.submits, 1, "上次任务没跑完时应重新提交")

    def test_done_segment_is_skipped_on_rerun(self) -> None:
        project = self.make_project(status=tasks.STATUS_DONE, output_path="whatever.mp4")
        client = FakeClient()
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=1, retry_backoff=0.0, timeout_seconds=10)
        self.assertEqual(counts.get(tasks.STATUS_DONE), 1)
        self.assertEqual(client.submits, 0)

    # ---------------- 台账与锁 ----------------

    def test_store_roundtrip_is_atomic(self) -> None:
        project = self.make_project()
        project.segments[0].history.append({"at": "now", "event": "x"})
        self.store.save(project)
        reloaded = self.store.load()
        self.assertEqual(reloaded.name, "batch-test")
        self.assertEqual(reloaded.segments[0].prompt, "测试提示词")
        self.assertEqual(reloaded.segments[0].history[0]["event"], "x")
        self.assertFalse(self.store.path.with_suffix(".json.tmp").exists(), "临时文件应已被替换")

    def test_lock_created_and_released(self) -> None:
        self.store.acquire_lock()
        self.assertTrue(self.store.lock_path.exists())
        payload = json.loads(self.store.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], __import__("os").getpid())
        self.store.release_lock()
        self.assertFalse(self.store.lock_path.exists())

    def test_dry_run_submits_nothing(self) -> None:
        project = self.make_project()
        client = FakeClient()
        runner = self.make_runner(client)
        counts = runner.run(project, max_attempts=1, retry_backoff=0.0, timeout_seconds=10,
                            dry_run=True)
        self.assertEqual(client.submits, 0)
        self.assertEqual(counts.get(tasks.STATUS_PENDING), 1)

    # ---------------- 参考图槽位按需加载 ----------------

    def test_segment_images_map_to_ref_slots(self) -> None:
        project = self.make_project(images=["char-a.png", "char-b.png"])
        runner = self.make_runner(FakeClient())
        values = runner.values_for(project, project.segments[0])
        self.assertEqual(values["ref_image_1"], "char-a.png")
        self.assertEqual(values["ref_image_2"], "char-b.png")
        self.assertNotIn("ref_image_3", values, "只用两张图时不应产生第三个槽位")

    def test_build_bypass_marks_unused_slots(self) -> None:
        target = {"overrides": {
            f"ref_image_{i}": {"node": 200 + i, "input": "image"} for i in range(1, 4)
        }}
        bypass = generate.build_bypass(target, {"ref_image_1": "x.png"})
        self.assertEqual(bypass, {202, 203}, "没给图的槽位应被跳过")
        self.assertEqual(generate.build_bypass(target, {}), {201, 202, 203})

    def test_runner_passes_bypass_to_converter(self) -> None:
        project = self.make_project(images=["only-one.png"])
        overrides = {"prompt": {"node": 1, "input": "value"}}
        overrides.update({
            f"ref_image_{i}": {"node": 200 + i, "input": "image"} for i in range(1, 3)
        })
        target = {"overrides": overrides}
        # 直接构造一个带参考图槽位的 runner
        ui = UiStub()
        runner = tasks.TaskRunner(
            client=FakeClient(), ui=ui, workflow_path=self.workflow_file,
            target_cfg=target, store=self.store, index=self.index,
            output_dir=self.output_dir, archive_root=self.archive_root,
            ffprobe=None, logger=self.log,
        )
        runner.run(project, max_attempts=1, retry_backoff=0.0, timeout_seconds=10)
        self.assertEqual(ui.last_bypass, {202}, "第二个槽位没有图，应传给转换器跳过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
