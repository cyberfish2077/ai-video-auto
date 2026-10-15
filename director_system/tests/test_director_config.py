"""阶段 1 的单测：目录、配置、密钥来源、生成限值的一致性。

运行方式（在项目根目录）::

    .venv\\Scripts\\python.exe -m unittest discover -s director_system/tests -t director_system/tests

这些测试把"2026-10-14 用户拍板的限值"钉住：谁改了 director.yaml 的关键数值，
测试会立刻失败，提醒同步审计报告与工作流核对。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg  # noqa: E402


class DirectorLayoutTest(unittest.TestCase):
    """阶段 1 的交付物必须都在。"""

    def test_required_dirs_exist(self) -> None:
        for name in ("config", "prompts", "schemas", "scripts", "tests", "reports"):
            self.assertTrue((DIRECTOR_ROOT / name).is_dir(), f"缺少目录 director_system/{name}/")

    def test_check_script_runs(self) -> None:
        """自检脚本本身要能跑通（跳过工作流核对，不依赖 ComfyUI）。"""
        script = DIRECTOR_ROOT / "scripts" / "check_director_env.py"
        result = subprocess.run(
            [sys.executable, str(script), "--no-workflow"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"自检脚本报错：\n{result.stdout}\n{result.stderr}")
        self.assertIn("导演系统", result.stdout)


class DirectorConfigTest(unittest.TestCase):
    """director.yaml 的结构与两条硬规矩。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conf = cfg.load_director_config()
        cls.text = cfg.DIRECTOR_CONFIG_FILE.read_text(encoding="utf-8")

    def test_config_matches_schema(self) -> None:
        import jsonschema

        schema_file = cfg.DIRECTOR_SCHEMAS_DIR / "director_config.schema.json"
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(self.conf), key=str)
        self.assertEqual([], [f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors])

    def test_no_absolute_path_in_director_config(self) -> None:
        """路径集中管理：导演系统配置里不许写绝对路径。"""
        hits = [
            line.strip()
            for line in self.text.splitlines()
            if re.match(r"^\s*[^#]*(^|\s)[A-Za-z]:[\\/]", line)
        ]
        self.assertEqual([], hits, "director.yaml 里出现了绝对路径（应改从 config/settings.yaml 取值）")

    def test_director_config_paths_live_in_config_module(self) -> None:
        """所有导演系统路径都由 video_auto.config 提供，业务代码不自己拼。"""
        self.assertEqual(PROJECT_ROOT / "director_system", cfg.DIRECTOR_DIR)
        for attr in ("DIRECTOR_SCHEMAS_DIR", "DIRECTOR_SCRIPTS_DIR", "DIRECTOR_TESTS_DIR",
                     "DIRECTOR_REPORTS_DIR", "DIRECTOR_SECRETS_DIR"):
            path = getattr(cfg, attr)
            self.assertTrue(str(path).startswith(str(cfg.DIRECTOR_DIR)), f"{attr} 必须在 director_system 下")

    def test_review_gates_are_three(self) -> None:
        """2026-10-14 用户决定：4 个闸门压成 3 个（A 选案 / B 制作前确认 / C 成片质检）。"""
        gates = self.conf["review_gates"]
        self.assertEqual(["A", "B", "C"], [g["id"] for g in gates])
        self.assertEqual("制作前确认", gates[1]["title"])
        self.assertIn("渲染", gates[1]["unlocks"])
        self.assertIn(self.conf["initial_gate_state"], self.conf["gate_states"])
        self.assertIn("PENDING_HUMAN_REVIEW", self.conf["gate_states"])

    def test_review_convergence_is_declared(self) -> None:
        """独立评审的收敛判据必须写在配置里（2026-10-15 加：追 0 条是无限循环）。"""
        rule = self.conf["review_convergence"]
        self.assertGreaterEqual(int(rule["max_revision_rounds"]), 1)
        self.assertEqual(0, int(rule["fatal_threshold"]))
        self.assertIn("放行", rule["release_rule"])

    def test_film_root_key_resolves(self) -> None:
        settings = cfg.load_settings()
        root = settings.path(self.conf["film_layout"]["root_key"])
        self.assertEqual(settings.path("paths.projects"), root)


class GenerationLimitsTest(unittest.TestCase):
    """把 2026-10-14 拍板的限值钉住（改这些数值必须同步审计报告）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.gen = cfg.load_director_config()["generation"]

    def test_fps_and_frames(self) -> None:
        self.assertEqual(24, self.gen["fps"])
        self.assertEqual({"min": 124, "max": 362, "step": 17, "min_mod": 5}, self.gen["frames"])

    def test_duration_range_matches_frames(self) -> None:
        frames = self.gen["frames"]
        fps = self.gen["fps"]
        self.assertAlmostEqual(frames["min"] / fps, self.gen["duration_seconds"]["min"], delta=0.05)
        self.assertAlmostEqual(frames["max"] / fps, self.gen["duration_seconds"]["max"], delta=0.05)

    def test_frame_step_math(self) -> None:
        """帧数只能取 step*k + min_mod；用审计报告里的 15 秒/362 帧案例验算。"""
        frames, step, mod = self.gen["frames"], self.gen["frames"]["step"], self.gen["frames"]["min_mod"]
        self.assertEqual(mod, frames["min"] % step)
        self.assertEqual(mod, frames["max"] % step)
        for k in (0, 1, 7, 21):
            n = step * k + mod
            self.assertEqual(mod, n % step)

    def test_resolution_and_ref_cap(self) -> None:
        self.assertEqual({"width": 1280, "height": 736, "megapixels": 0.9}, self.gen["resolution"])
        self.assertEqual(9, self.gen["ref_images_max"])


class WorkflowRegistryTest(unittest.TestCase):
    """登记表与导演系统配置必须一致（不启动 ComfyUI 也能查的部分）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = cfg.load_workflow_targets()
        cls.conf = cfg.load_director_config()

    def test_target_registered_and_file_exists(self) -> None:
        name = self.conf["generation"]["target"]
        self.assertIn(name, self.targets, f"登记表里没有 {name}")
        workflow_file = cfg.PROJECT_ROOT / str(self.targets[name]["file"])
        self.assertTrue(workflow_file.is_file(), f"工作流文件不存在：{workflow_file}")

    def test_ref_slots_match_cap(self) -> None:
        overrides = self.targets[self.conf["generation"]["target"]]["overrides"]
        slots = [k for k in overrides if str(k).startswith("ref_image_")]
        self.assertEqual(self.conf["generation"]["ref_images_max"], len(slots))

    def test_megapixels_agreed(self) -> None:
        defaults = self.targets[self.conf["generation"]["target"]].get("defaults") or {}
        self.assertEqual(
            float(self.conf["generation"]["resolution"]["megapixels"]),
            float(defaults["megapixels"]),
            "登记表的 defaults.megapixels 与 director.yaml 不一致（它决定实际生成分辨率）",
        )

    def test_workflow_reference_is_not_the_missing_clip(self) -> None:
        """工作流原文件指向已删除的 int8_convrot 编码器，登记表必须给出替代。"""
        defaults = self.targets[self.conf["generation"]["target"]].get("defaults") or {}
        self.assertIn("clip_name", defaults)
        self.assertNotIn("int8_convrot", str(defaults["clip_name"]))


class SecretsTest(unittest.TestCase):
    """密钥只从环境变量或本地（被忽略的）文件读。"""

    def test_secrets_section_points_to_env_and_local_file(self) -> None:
        secrets = cfg.load_director_config()["secrets"]
        self.assertEqual("DEEPSEEK_API_KEY", secrets["env_var"])
        local = PROJECT_ROOT / str(secrets["local_file"])
        self.assertTrue(str(local).startswith(str(cfg.DIRECTOR_DIR)), "本地密钥文件必须位于 director_system 内")

    def test_gitignore_covers_secrets_dir(self) -> None:
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("director_system/secrets/", ignored)
        self.assertIn(".env", ignored)


if __name__ == "__main__":
    unittest.main()
