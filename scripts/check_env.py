"""环境检查程序（阶段三）。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe scripts\\check_env.py

它逐项检查运行环境，输出一份中文报告，并在结尾告诉你下一步做什么。

除"写权限探测"（会在项目自己的输出目录里建一个临时文件并立即删除）之外，
本程序不做任何修改；不会改动 ComfyUI、不会安装任何东西。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# 统一按 UTF-8 输出：真实控制台下行为不变，重定向/管道时中文不再乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 编码设置失败不应中断检查
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

OK = "OK"
WARN = "警告"
ERR = "错误"

_results: list[tuple[str, str, str]] = []  # (级别, 标题, 详情)


def record(level: str, title: str, detail: str = "") -> None:
    _results.append((level, title, detail))


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """执行命令，返回 (退出码, 输出文本)。失败不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"找不到命令：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout} 秒）：{' '.join(cmd)}"


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _satisfies(version: tuple[int, ...], spec: str) -> bool:
    """支持形如 ">=3.12,<3.14" 的简单版本区间判断。"""
    for rule in spec.split(","):
        rule = rule.strip()
        if not rule:
            continue
        if rule.startswith(">="):
            if version < _parse_version(rule[2:]):
                return False
        elif rule.startswith("<="):
            if version > _parse_version(rule[2:]):
                return False
        elif rule.startswith(">"):
            if version <= _parse_version(rule[1:]):
                return False
        elif rule.startswith("<"):
            if version >= _parse_version(rule[1:]):
                return False
        elif rule.startswith("=="):
            if version != _parse_version(rule[2:]):
                return False
    return True


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------


def check_python(required_spec: str) -> None:
    version = sys.version_info[:3]
    version_text = ".".join(str(x) for x in version)
    in_venv = sys.prefix != sys.base_prefix

    if _satisfies(version, required_spec):
        record(OK, f"Python 版本 {version_text}", f"要求 {required_spec}，解释器：{sys.executable}")
    else:
        record(
            ERR,
            f"Python 版本 {version_text} 不符合要求",
            f"要求 {required_spec}。解决办法：用 D:\\Python312\\python.exe -m venv .venv 重建虚拟环境。",
        )

    if in_venv:
        record(OK, "运行在虚拟环境内", sys.prefix)
    else:
        record(
            WARN,
            "当前不在虚拟环境内运行",
            "建议改用 .venv\\Scripts\\python.exe scripts\\check_env.py，"
            "避免依赖装到别处。",
        )


def check_packages() -> None:
    from importlib.metadata import PackageNotFoundError, version

    for module, display in (("yaml", "PyYAML"), ("dotenv", "python-dotenv"), ("requests", "requests")):
        try:
            __import__(module)
        except ImportError:
            record(
                ERR,
                f"缺少依赖库 {display}",
                "解决办法：.venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            )
            continue
        try:
            ver = version(display)
        except PackageNotFoundError:
            ver = "已安装（版本号读取失败）"
        record(OK, f"依赖库 {display}", f"版本 {ver}")


def check_git() -> None:
    code, out = _run(["git", "--version"])
    if code != 0:
        record(ERR, "Git 不可用", "解决办法：确认 Git 已安装且在 PATH 中（C:\\Program Files\\Git\\cmd）。")
        return
    record(OK, "Git 可用", out.strip().splitlines()[0])

    code, out = _run(["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"])
    if code != 0:
        advice = "解决办法：在管理员 PowerShell 里执行 icacls \"D:\\AI_Video_Auto\" /setowner \"%USERNAME%\" /T 修正属主。"
        if "dubious ownership" in out:
            advice = (
                "原因：目录属主与当前账号不一致。"
                "解决办法：git config --global --add safe.directory D:/AI_Video_Auto，"
                "或以管理员身份执行 icacls \"D:\\AI_Video_Auto\" /setowner \"%USERNAME%\" /T"
            )
        record(ERR, "Git 无法识别本仓库", advice)
        return
    dirty = [line for line in out.splitlines() if line.strip()]
    if dirty:
        record(WARN, f"Git 工作区有 {len(dirty)} 个未提交改动", "确认无误后执行 git add -A 与 git commit 存档。")
    else:
        record(OK, "Git 仓库状态干净", str(PROJECT_ROOT))

    code, out = _run(["git", "-C", str(PROJECT_ROOT), "check-ignore", ".env"])
    if code == 0:
        record(OK, ".env 已被 Git 忽略", "密钥不会进入版本库")
    else:
        record(
            ERR,
            ".env 未被 Git 忽略，存在密钥泄露风险",
            "解决办法：在 .gitignore 里加入一行 .env，然后执行 git rm --cached .env。",
        )


def check_ffmpeg(ffmpeg: Path, ffprobe: Path) -> None:
    for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        if not path.exists():
            record(
                ERR,
                f"{name} 路径不存在：{path}",
                "解决办法：确认 C:\\ffmpeg\\bin 下的文件存在，或修改 config/settings.yaml 中的 ffmpeg 段。",
            )
            continue
        code, out = _run([str(path), "-version"])
        if code != 0:
            record(ERR, f"{name} 无法执行", out.strip()[:200])
            continue
        first = out.strip().splitlines()[0] if out.strip() else ""
        record(OK, f"{name} 可用", first)


def check_comfyui(base_url: str) -> dict | None:
    try:
        import requests
    except ImportError:
        return None
    try:
        stats = requests.get(base_url.rstrip("/") + "/system_stats", timeout=8).json()
    except Exception as exc:  # noqa: BLE001 - 需要把各种网络异常都汇报给用户
        record(
            ERR,
            f"ComfyUI 无法访问：{base_url}",
            f"详细信息：{exc}\n解决办法：确认 ComfyUI 已启动，"
            "且地址与 config/settings.yaml 里的 comfyui.base_url 一致。",
        )
        return None

    system = stats.get("system", {})
    record(
        OK,
        f"ComfyUI 可访问（版本 {system.get('comfyui_version', '未知')}）",
        f"地址 {base_url}，系统 {system.get('os', '?')}",
    )

    try:
        queue = requests.get(base_url.rstrip("/") + "/queue", timeout=8).json()
        running = len(queue.get("queue_running", []))
        pending = len(queue.get("queue_pending", []))
        if running or pending:
            record(WARN, f"ComfyUI 队列非空闲（运行中 {running}，等待 {pending}）", "等队列清空后再提交任务。")
        else:
            record(OK, "ComfyUI 队列空闲", "可以安全提交新任务")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "无法读取 ComfyUI 队列", str(exc))
    return stats


def check_workflow(workflow_path: Path) -> None:
    if workflow_path.exists():
        size_kb = workflow_path.stat().st_size / 1024
        record(OK, "主力工作流文件已就位", f"{workflow_path}（{size_kb:.1f} KB）")
    else:
        record(
            WARN,
            "主力工作流尚未导入 workflows/",
            f"期望位置：{workflow_path}\n"
            "说明：阶段五会导入并校验，此处仅提示，不阻塞。",
        )


def check_workspace(settings) -> None:
    for key in ("paths.data", "paths.logs", "paths.cache", "paths.projects"):
        path = settings.path(key)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            record(OK, f"已创建目录 {key}", str(path))
        probe = path / "_write_test.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            record(OK, f"目录可写 {key}", str(path))
        except OSError as exc:
            record(ERR, f"目录不可写 {key}", f"{path}\n{exc}")


def check_disk() -> None:
    for drive in ("D:\\", "C:\\"):
        try:
            usage = shutil.disk_usage(drive)
        except OSError as exc:
            record(WARN, f"无法读取 {drive} 磁盘信息", str(exc))
            continue
        free_gb = usage.free / 1024**3
        level = OK if free_gb >= 20 else WARN
        advice = "" if level == OK else "可用空间偏少，长视频分段与成片建议及时清理或转移到其他盘。"
        record(level, f"{drive} 剩余空间 {free_gb:.1f} GB", advice)


def check_secrets() -> None:
    from video_auto import config as cfg

    if not cfg.ENV_FILE.exists():
        record(
            ERR,
            "缺少 .env 文件",
            f"解决办法：复制 {cfg.ENV_EXAMPLE_FILE} 为 {cfg.ENV_FILE}，填入 DeepSeek 密钥。",
        )
        return
    record(OK, ".env 文件存在", str(cfg.ENV_FILE))

    try:
        key = cfg.get_secret("DEEPSEEK_API_KEY")
    except cfg.ConfigError as exc:
        record(ERR, "DeepSeek 密钥未配置", str(exc))
        return
    record(OK, "DeepSeek 密钥已配置", f"DEEPSEEK_API_KEY = {cfg.mask_secret(key)}（此处为掩码，完整值不打印）")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    print("=" * 68)
    print("AI 长视频自动化系统 — 环境检查（阶段三）")
    print("=" * 68)

    try:
        from video_auto import config as cfg
    except ImportError as exc:
        print(f"[错误] 无法导入项目包 video_auto：{exc}")
        print("解决办法：确认在项目根目录执行，且 src/video_auto/__init__.py 存在。")
        return 2

    try:
        settings = cfg.load_settings()
    except cfg.ConfigError as exc:
        print(f"[错误] {exc}")
        return 2

    print(f"项目根目录：{cfg.PROJECT_ROOT}")
    print(f"配置文件　：{settings.source}")
    print(f"项目名称　：{settings.project_name}")
    print()

    check_python(str(settings.get("python.required", ">=3.12,<3.14")))
    check_packages()
    check_secrets()
    check_git()
    check_ffmpeg(settings.path("ffmpeg.ffmpeg"), settings.path("ffmpeg.ffprobe"))
    check_comfyui(str(settings.get("comfyui.base_url")))
    check_workflow(settings.path("comfyui.main_workflow"))
    check_workspace(settings)
    check_disk()

    print("-" * 68)
    for level, title, detail in _results:
        print(f"[{level}] {title}")
        if detail:
            for line in str(detail).splitlines():
                print(f"        {line}")

    errors = sum(1 for level, _, _ in _results if level == ERR)
    warnings = sum(1 for level, _, _ in _results if level == WARN)
    print("-" * 68)
    print(f"汇总：{len(_results)} 项检查，其中错误 {errors} 项、警告 {warnings} 项。")

    if errors:
        print("结论：环境尚未就绪，请先按上面的 [错误] 提示修复，然后重新运行本检查。")
        return 1
    print("结论：环境就绪。当前进度与下一步请查看 references/实测经验与坑.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
