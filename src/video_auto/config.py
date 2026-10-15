"""全局配置加载（阶段三）。

设计原则
--------
* 机密信息（API 密钥）只从 ``.env`` 或环境变量读取；
* 非机密配置（路径、地址、模型名）只从 ``config/settings.yaml`` 读取；
* 业务代码不允许自己打开配置文件、也不允许硬编码绝对路径，
  统一通过 :func:`load_settings` 和 :func:`get_secret` 取值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

#: 项目根目录（本文件位于 <root>/src/video_auto/config.py）
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

CONFIG_DIR: Path = PROJECT_ROOT / "config"
SETTINGS_FILE: Path = CONFIG_DIR / "settings.yaml"
SETTINGS_EXAMPLE_FILE: Path = CONFIG_DIR / "settings.example.yaml"
WORKFLOW_TARGETS_FILE: Path = CONFIG_DIR / "workflow_targets.yaml"
STYLES_FILE: Path = CONFIG_DIR / "styles.yaml"
ENV_FILE: Path = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE: Path = PROJECT_ROOT / ".env.example"

#: 导演系统（director_system/）的配置。路径仍然只在本模块里集中定义，
#: 业务代码一律通过 :func:`load_director_config` 取值，不直接读 yaml。
DIRECTOR_DIR: Path = PROJECT_ROOT / "director_system"
DIRECTOR_CONFIG_FILE: Path = DIRECTOR_DIR / "config" / "director.yaml"
DIRECTOR_CONFIG_EXAMPLE_FILE: Path = DIRECTOR_DIR / "config" / "director.example.yaml"
#: 导演系统的 JSON Schema 目录（阶段 4/5 会往里面加分镜、提示词等 schema）
DIRECTOR_SCHEMAS_DIR: Path = DIRECTOR_DIR / "schemas"
#: 导演系统的自检脚本、测试与报告目录
DIRECTOR_SCRIPTS_DIR: Path = DIRECTOR_DIR / "scripts"
DIRECTOR_TESTS_DIR: Path = DIRECTOR_DIR / "tests"
DIRECTOR_REPORTS_DIR: Path = DIRECTOR_DIR / "reports"
#: 导演系统的本地密钥文件（已被 .gitignore 忽略；只报"有没有"，不读内容）
DIRECTOR_SECRETS_DIR: Path = DIRECTOR_DIR / "secrets"


class ConfigError(RuntimeError):
    """配置读取失败。错误信息中一定附带"怎么修"的提示。"""


def _read_yaml(path: Path, example: Path | None = None) -> dict[str, Any]:
    if not path.exists():
        fix = (
            f"解决办法：把 {example} 复制为 {path}，再按需修改。"
            if example
            else f"解决办法：确认该文件存在且未被误删：{path}"
        )
        raise ConfigError(
            f"找不到配置文件：{path}\n{fix}"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # YAML 语法错误
        raise ConfigError(
            f"配置文件 YAML 格式有误：{path}\n"
            f"解析器报错：{exc}\n"
            f"解决办法：检查缩进（只能用空格）和中文冒号，冒号后面要有一个空格。"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件最外层必须是「键: 值」结构：{path}")
    return data


@dataclass(frozen=True)
class Settings:
    """只读配置对象。用点号路径取值，例如 ``settings.get("comfyui.base_url")``。"""

    raw: dict[str, Any]
    source: Path

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is not None:
                    return default
                raise ConfigError(
                    f"配置项缺失：{dotted_key}（来自 {self.source}）\n"
                    f"解决办法：在配置文件里补上该项，可对照 settings.example.yaml。"
                )
            node = node[part]
        return node

    def path(self, dotted_key: str, *, must_exist: bool = False) -> Path:
        """取路径型配置并转换为 :class:`Path`；相对路径按项目根目录解析。"""
        raw_value = str(self.get(dotted_key))
        path = Path(raw_value)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if must_exist and not path.exists():
            raise ConfigError(
                f"配置项 {dotted_key} 指向的路径不存在：{path}\n"
                f"解决办法：确认该目录/文件确实存在，或修正 config/settings.yaml。"
            )
        return path

    @property
    def project_name(self) -> str:
        return str(self.get("project.name", "AI_Video_Auto"))


def load_settings() -> Settings:
    """读取并返回全局配置。"""
    return Settings(raw=_read_yaml(SETTINGS_FILE, SETTINGS_EXAMPLE_FILE), source=SETTINGS_FILE)


def load_workflow_targets() -> dict[str, Any]:
    """读取 config/workflow_targets.yaml：每份工作流的参数改写点登记表。"""
    data = _read_yaml(WORKFLOW_TARGETS_FILE)
    if not isinstance(data, dict) or not data:
        raise ConfigError(
            f"工作流登记表为空或格式不对：{WORKFLOW_TARGETS_FILE}\n"
            f"解决办法：按 config/workflow_targets.yaml 的现有格式补充条目。"
        )
    return data


def load_director_config() -> dict[str, Any]:
    """读取 director_system/config/director.yaml：导演系统自己的配置。

    这里**只放导演系统的知识**（审核关卡、生成能力限值、密钥来源等）。
    所有**路径**继续由本模块的 :class:`Settings`（config/settings.yaml）提供，
    导演系统的配置文件里不写任何绝对路径。
    """
    data = _read_yaml(DIRECTOR_CONFIG_FILE, DIRECTOR_CONFIG_EXAMPLE_FILE)
    if not isinstance(data, dict) or not data:
        raise ConfigError(
            f"导演系统配置为空或格式不对：{DIRECTOR_CONFIG_FILE}\n"
            f"解决办法：补上 version / review_gates / film_layout / generation / secrets 这几节，"
            f"结构见 director_system/schemas/director_config.schema.json。"
        )
    return data


def load_style(name: str | None = None) -> tuple[str, dict[str, Any]]:
    """读取 config/styles.yaml 里的一个风格档位，返回 ``(档位名, 档位内容)``。

    ``name`` 为空时用文件里的 ``default_profile``。档位内容统一补齐五个键
    （``style_prefix`` / ``bgm_line`` / ``negative`` / ``story_style`` / ``material_style``），
    缺的按空字符串处理，这样调用方不必自己判空。
    """
    data = _read_yaml(STYLES_FILE)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError(
            f"风格档位表为空或格式不对：{STYLES_FILE}\n"
            f"解决办法：按 config/styles.yaml 的现有格式，在 profiles 下补一个档位。"
        )
    chosen = (name or str(data.get("default_profile") or "")).strip()
    if not chosen:
        raise ConfigError(
            f"没有指定风格档位，且 {STYLES_FILE} 里没有 default_profile。\n"
            f"解决办法：加 --style <档位名>，可选：{'、'.join(sorted(profiles))}。"
        )
    if chosen not in profiles:
        raise ConfigError(
            f"没有这个风格档位：{chosen}（来自 {STYLES_FILE}）\n"
            f"现有档位：{'、'.join(sorted(profiles))}\n"
            f"解决办法：改用上面列出的档位名，或在 {STYLES_FILE} 的 profiles 下新增一个档位。"
        )
    raw = profiles[chosen] or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"风格档位 {chosen} 的内容格式不对（应是「键: 值」结构）：{STYLES_FILE}\n"
            f"解决办法：检查该档位下的缩进。"
        )
    profile = {key: str(raw.get(key) or "").strip()
               for key in ("style_prefix", "bgm_line", "negative",
                           "story_style", "material_style")}
    if not profile["style_prefix"]:
        raise ConfigError(
            f"风格档位 {chosen} 缺少 style_prefix（每段提示词的风格行）。\n"
            f"解决办法：在 {STYLES_FILE} 里给该档位补上 style_prefix。"
        )
    return chosen, profile


def load_env() -> None:
    """加载项目根目录的 ``.env``。

    已存在的环境变量优先（``override=False``），这样临时用命令行设置的变量不会被文件覆盖。
    """
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)


def get_secret(name: str, *, required: bool = True) -> str:
    """读取密钥类配置（只从环境变量 / .env，绝不写进源码）。"""
    load_env()
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ConfigError(
                f"密钥缺失：环境变量 {name} 为空\n"
                f"解决办法：复制 {ENV_EXAMPLE_FILE.name} 为 {ENV_FILE.name}，"
                f"填入真实密钥后重试（{ENV_FILE.name} 已被 Git 忽略，不会提交）。"
            )
        return ""
    return value


def mask_secret(value: str) -> str:
    """把密钥变成可以安全打印的形式，例如 ``sk-00a****2699``。"""
    if not value:
        return "(空)"
    if len(value) <= 10:
        return value[:2] + "****"
    return f"{value[:6]}****{value[-4:]}"
