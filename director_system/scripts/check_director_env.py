"""导演系统环境自检（阶段 1）。

只读：不创建、不修改、不删除任何文件；不提交 ComfyUI 任务；不调用 DeepSeek；
**不读取任何密钥值**（只报"有没有配置"）。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe director_system\\scripts\\check_director_env.py
    .venv\\Scripts\\python.exe director_system\\scripts\\check_director_env.py --no-workflow

退出码：``0`` = 没有错误（可能有警告）；``1`` = 有错误，每条错误都会打印"下一步怎么办"。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

DIRECTOR_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DIRECTOR_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg  # noqa: E402

#: 规范包自带的提示词与规范文件（阶段 1 只核对"在不在"）
REQUIRED_PROMPTS = [
    "01_idea_development.md",
    "02_story_outline.md",
    "03_screenplay.md",
    "04_storyboard.md",
    "05_prompt_compiler.md",
    "06_quality_review.md",
]
REQUIRED_STANDARDS = [
    "通用剧本与分镜规范.md",
    "文戏提示词规范.md",
    "武戏提示词规范.md",
]

#: 导演系统必须存在的子目录（阶段 1 的交付内容）
REQUIRED_SUBDIRS = ["config", "prompts", "schemas", "scripts", "tests", "reports"]

#: 本地密钥文件必须被 .gitignore 覆盖的条目
GITIGNORE_SECRET_RULE = "director_system/secrets/"


class Report:
    """收集检查结果；错误必须带"下一步建议"。"""

    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.oks: list[str] = []

    def ok(self, title: str) -> None:
        self.oks.append(title)
        print(f"[OK  ] {title}")

    def warn(self, title: str, advice: str = "") -> None:
        self.warnings.append((title, advice))
        print(f"[警告] {title}")
        if advice:
            print(f"        下一步：{advice}")

    def error(self, title: str, advice: str = "") -> None:
        self.errors.append((title, advice))
        print(f"[错误] {title}")
        if advice:
            print(f"        下一步：{advice}")

    def section(self, title: str) -> None:
        print()
        print("=" * 70)
        print(title)
        print("=" * 70)

    def summary(self) -> int:
        self.section("自检结果")
        print(f"通过 {len(self.oks)} 项，警告 {len(self.warnings)} 项，错误 {len(self.errors)} 项")
        if self.errors:
            print()
            print("必须先解决（每条都带下一步建议）：")
            for title, advice in self.errors:
                print(f"  - {title}")
                if advice:
                    print(f"    → {advice}")
        if not self.errors:
            print()
            print("结论：导演系统阶段 1 的目录与配置自检通过。")
        return 1 if self.errors else 0


def check_dirs(rep: Report) -> None:
    rep.section("1. 目录结构（阶段 1 的交付内容）")
    for name in REQUIRED_SUBDIRS:
        path = DIRECTOR_ROOT / name
        if path.is_dir():
            rep.ok(f"目录存在：director_system/{name}/")
        else:
            rep.error(
                f"目录缺失：director_system/{name}/",
                f"新建该目录（New-Item -ItemType Directory -Force -Path \"{path}\"）后重新自检。",
            )


def check_docs(rep: Report) -> None:
    rep.section("2. 提示词与规范文件")
    missing: list[str] = []
    for name in REQUIRED_PROMPTS:
        if not (DIRECTOR_ROOT / "prompts" / name).is_file():
            missing.append(f"prompts/{name}")
    for name in REQUIRED_STANDARDS:
        if not (DIRECTOR_ROOT / "standards" / name).is_file():
            missing.append(f"standards/{name}")
    if missing:
        rep.error(
            f"缺少 {len(missing)} 个规范包文件：" + "，".join(missing),
            "从规范包重新解压这些文件到对应目录；缺文件会导致后面的提示词编译没有依据。",
        )
    else:
        rep.ok(f"提示词 {len(REQUIRED_PROMPTS)} 份、规范 {len(REQUIRED_STANDARDS)} 份齐全")


def check_config(rep: Report) -> dict | None:
    """校验 director.yaml 是否符合 schema（用 jsonschema，真校验）。"""
    rep.section("3. 导演系统配置（director.yaml）")
    try:
        conf = cfg.load_director_config()
    except Exception as exc:  # noqa: BLE001
        rep.error(f"读取配置失败：{exc}", "按报错里的路径与格式提示修正 director.yaml。")
        return None

    schema_file = cfg.DIRECTOR_SCHEMAS_DIR / "director_config.schema.json"
    try:
        import jsonschema
        from importlib.metadata import version as _pkg_version
    except ImportError:
        rep.error(
            "缺少 jsonschema，无法校验配置",
            "运行 .venv\\Scripts\\python.exe -m pip install jsonschema（项目已把它记进 requirements.txt）。",
        )
        return conf

    try:
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        rep.error(
            f"找不到配置 schema：{schema_file}",
            "确认 director_system/schemas/director_config.schema.json 存在。",
        )
        return conf

    validator = jsonschema.Draft202012Validator(schema)
    problems = sorted(validator.iter_errors(conf), key=lambda e: list(e.path))
    if problems:
        detail = "；".join(
            f"{'/'.join(str(p) for p in err.path) or '(根)'}：{err.message}"
            for err in problems[:5]
        )
        rep.error(
            f"director.yaml 不符合 schema（{len(problems)} 处）：{detail}",
            "按 director_system/schemas/director_config.schema.json 修正字段；"
            "限值不要手改，先跑一次工作流核对。",
        )
    else:
        rep.ok(f"director.yaml 通过 schema 校验（jsonschema {_pkg_version('jsonschema')}）")
    return conf


def check_paths(rep: Report, conf: dict | None) -> None:
    """核对"路径只来自 settings.yaml"这条硬规则是否还成立。"""
    rep.section("4. 路径来源与影片目录")
    try:
        settings = cfg.load_settings()
    except Exception as exc:  # noqa: BLE001
        rep.error(f"读取 config/settings.yaml 失败：{exc}", "修正 config/settings.yaml 后重新自检。")
        return

    root_key = (conf or {}).get("film_layout", {}).get("root_key", "paths.projects")
    try:
        projects_root = settings.path(root_key)
    except Exception as exc:  # noqa: BLE001
        rep.error(
            f"settings.yaml 里取不到 {root_key}：{exc}",
            f"在 config/settings.yaml 里补上 {root_key}（对照 settings.example.yaml）。",
        )
        return

    if not projects_root.is_dir():
        rep.warn(
            f"影片根目录还不存在：{projects_root}",
            "第一次创建影片时程序会自动建立；也可以手动新建该目录。",
        )
        return

    films = [p.name for p in projects_root.iterdir() if p.is_dir() and not p.name.endswith("-materials")]
    rep.ok(f"影片根目录可用（来自 settings.yaml 的 {root_key}）：{projects_root.name}\\，现有 {len(films)} 个影片目录")

    # 硬规则：配置里不许出现绝对路径（Windows 盘符）
    conf_file = cfg.DIRECTOR_CONFIG_FILE
    text = conf_file.read_text(encoding="utf-8")
    hits = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^\s*[^#]*(^|\s)[A-Za-z]:[\\/]", line)
    ]
    if hits:
        rep.error(
            f"director.yaml 里出现了绝对路径：{hits[0][:60]}",
            "删掉它，改从 config/settings.yaml 取值（项目硬性规则：路径集中管理）。",
        )
    else:
        rep.ok("director.yaml 里没有绝对路径（符合「路径集中管理」）")


def check_secrets(rep: Report, conf: dict | None) -> None:
    """只报"有没有配置"，绝不读取、打印密钥值。"""
    rep.section("5. 密钥来源（只检查方式，不读值）")
    secrets_cfg = (conf or {}).get("secrets") or {}
    env_var = str(secrets_cfg.get("env_var", "DEEPSEEK_API_KEY"))
    local_rel = str(secrets_cfg.get("local_file", "director_system/secrets/local.env"))
    local_path = PROJECT_ROOT / local_rel

    try:
        cfg.load_env()
    except Exception as exc:  # noqa: BLE001
        rep.warn(f"加载 .env 失败：{exc}", "确认项目根目录的 .env 语法正确（本程序不会读取它的内容）。")

    env_set = bool(os.environ.get(env_var, "").strip())
    file_set = local_path.is_file()

    if env_set:
        rep.ok(f"环境变量 {env_var} 已配置（值未读取）")
    else:
        rep.warn(
            f"环境变量 {env_var} 未配置",
            f"阶段 2 起需要它：复制 .env.example 为 .env 并填入密钥；"
            f"或把密钥写进本地文件 {local_rel}（两者都不进 Git）。",
        )
    if file_set:
        rep.ok(f"本地密钥文件存在：{local_rel}（内容未读取）")

    gitignore = PROJECT_ROOT / ".gitignore"
    try:
        ignored = gitignore.read_text(encoding="utf-8")
    except FileNotFoundError:
        rep.error("项目根目录没有 .gitignore", "创建 .gitignore 并加入密钥与生成物的忽略规则。")
        return
    if GITIGNORE_SECRET_RULE in ignored:
        rep.ok(f".gitignore 已忽略导演系统密钥目录（{GITIGNORE_SECRET_RULE}）")
    else:
        rep.error(
            f".gitignore 没有忽略 {GITIGNORE_SECRET_RULE}",
            f"在 .gitignore 里补上一行 {GITIGNORE_SECRET_RULE}，否则本地密钥可能被提交。",
        )


def _load_object_info(rep: Report) -> dict | None:
    """优先实时拉取；ComfyUI 没开就退回本地缓存并提醒。"""
    settings = cfg.load_settings()
    base_url = str(settings.get("comfyui.base_url"))
    cache_file = settings.path("paths.cache") / "object_info_cache.json"
    try:
        from video_auto import comfy

        return comfy.load_object_info(base_url, cache_file)
    except Exception as exc:  # noqa: BLE001
        if cache_file.is_file():
            rep.warn(
                f"ComfyUI 未响应（{exc.__class__.__name__}），改用本地缓存核对节点",
                "要拿到最新节点定义，先启动 ComfyUI 再跑一次自检。",
            )
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as bad:
                rep.warn(f"本地节点缓存损坏：{bad}", "删除该缓存文件后重新自检（会重新拉取）。")
                return None
        rep.warn(
            f"拿不到节点定义表：{exc}",
            "启动 ComfyUI 后重新自检，否则无法核对工作流参数。",
        )
        return None


def check_workflow(rep: Report, conf: dict, object_info: dict | None) -> None:
    """核对 director.yaml 的限值是否还和真实工作流一致。"""
    gen = conf["generation"]
    target_name = str(gen["target"])

    targets = cfg.load_workflow_targets()
    target = targets.get(target_name)
    if not target:
        rep.error(
            f"config/workflow_targets.yaml 里没有工作流 {target_name}（director.yaml 指定的目标）",
            f"在登记表里补上 {target_name}，或把 director.yaml 的 generation.target 改成已登记的名字。",
        )
        return
    rep.ok(f"工作流登记项存在：{target_name}")

    workflow_file = cfg.PROJECT_ROOT / str(target["file"])
    if not workflow_file.is_file():
        rep.error(
            f"工作流文件不存在：{workflow_file}",
            "确认 workflows\\ 下确实有这份文件（不要改工作流内容）。",
        )
        return
    rep.ok(f"工作流文件存在：{target['file']}")

    # (a) 文本编码器：用 object_info 的真实下拉选项核对，不猜目录
    clip_name = str((target.get("defaults") or {}).get("clip_name", ""))
    if not clip_name:
        rep.warn("登记表没有 defaults.clip_name", "补上该工作流固定使用的文本编码器文件名。")
    elif object_info is None:
        rep.warn("未核对文本编码器", "启动 ComfyUI 后重跑，确认 defaults.clip_name 是现存的模型。")
    else:
        options = (
            (object_info.get("CLIPLoader", {}).get("input", {}).get("required", {}).get("clip_name") or [[]])[0]
        )
        if clip_name in options:
            rep.ok(f"文本编码器存在：{clip_name}")
        else:
            rep.error(
                f"文本编码器不存在：{clip_name}",
                "工作流原文件里写的是已删除的 int8_convrot 版；请把登记表的 defaults.clip_name "
                "改成 ComfyUI 现存的那份（用 /object_info 的 CLIPLoader 下拉选项确认）。",
            )

    # (b) 帧数区间 / 步进 / 参考图上限：与节点 schema 对表
    if object_info is None:
        rep.warn("未核对节点级限值", "启动 ComfyUI 后重跑。")
    else:
        length_spec = (
            object_info.get("MiniMaxH3ReferenceToVideo", {})
            .get("input", {})
            .get("required", {})
            .get("length")
        )
        if isinstance(length_spec, list) and len(length_spec) > 1:
            spec = length_spec[1] or {}
            frames = gen["frames"]
            # 注意区分两套数：节点硬限值（min/max/step，model 能接受的范围）
            # 与 tooltip 里写明的**训练区间**（124~362）。director.yaml 记的是训练区间，
            # 因为超出训练区间虽然能跑通，画面质量没有保证。
            mismatched: list[str] = []
            if spec.get("step") is not None and int(spec["step"]) != frames["step"]:
                mismatched.append(f"步进（节点={spec['step']} / 配置={frames['step']}）")
            tooltip = str(spec.get("tooltip", ""))
            trained = re.search(r"trained range is ~(\d+)\s*-\s*(\d+)", tooltip)
            if trained:
                lo, hi = int(trained.group(1)), int(trained.group(2))
                if lo != frames["min"]:
                    mismatched.append(f"训练区间下界（节点={lo} / 配置={frames['min']}）")
                if hi != frames["max"]:
                    mismatched.append(f"训练区间上界（节点={hi} / 配置={frames['max']}）")
            else:
                rep.warn(
                    "节点 tooltip 里读不到训练区间（trained range）",
                    f"人工确认 {frames['min']}~{frames['max']} 帧这个区间，必要时修正 director.yaml。",
                )
            if spec.get("min") is not None and frames["min"] < int(spec["min"]):
                mismatched.append(f"配置下界低于节点硬下限（节点={spec['min']} / 配置={frames['min']}）")
            if spec.get("max") is not None and frames["max"] > int(spec["max"]):
                mismatched.append(f"配置上界高于节点硬上限（节点={spec['max']} / 配置={frames['max']}）")
            for edge in ("min", "max"):
                if frames[edge] % frames["step"] != frames["min_mod"]:
                    mismatched.append(
                        f"frames.{edge}={frames[edge]} 不满足「step 的整数倍 + {frames['min_mod']}」"
                    )
            if mismatched:
                rep.error(
                    "帧数限值与节点不一致：" + "；".join(mismatched),
                    "以工作流实测为准修正 director.yaml，并同步更新审计报告第 5 节。",
                )
            else:
                rep.ok(
                    f"帧数限值与节点一致（训练区间 {frames['min']}~{frames['max']}，步进 {frames['step']}；"
                    f"节点硬限值 {spec.get('min')}~{spec.get('max')}）"
                )
        else:
            rep.warn("节点 schema 里读不到 length 规格", "确认 comfyui-videohelpersuite / H3 插件版本未变。")

        ref_spec = (
            object_info.get("MiniMaxH3ReferenceToVideo", {})
            .get("input", {})
            .get("optional", {})
            .get("ref_images")
        )
        if isinstance(ref_spec, list) and len(ref_spec) > 1:
            # 动态槽位的 min/max 写在 template 里（实测：spec["template"]["max"] == 9）
            template = (ref_spec[1] or {}).get("template") or {}
            max_ref = template.get("max")
            if max_ref is None:
                rep.warn(
                    "读不到参考图槽位上限（schema 结构可能变了）",
                    f"人工确认 ref_images 的 max，再核对 director.yaml 的 {gen['ref_images_max']}。",
                )
            elif max_ref == gen["ref_images_max"]:
                rep.ok(f"参考图上限与节点一致（{max_ref} 张）")
            else:
                rep.error(
                    f"参考图上限不一致：节点={max_ref} / 配置={gen['ref_images_max']}",
                    "修正 director.yaml 的 generation.ref_images_max。",
                )

    # (c) 登记表里登记的参考图槽位数
    registered = [k for k in (target.get("overrides") or {}) if str(k).startswith("ref_image_")]
    if len(registered) == gen["ref_images_max"]:
        rep.ok(f"登记表已登记 {len(registered)} 个参考图槽位（与上限一致）")
    else:
        rep.error(
            f"登记表登记了 {len(registered)} 个参考图槽位，但上限是 {gen['ref_images_max']}",
            "补齐/删减 t8bridge_9ref 的 ref_image_N 条目，让槽位与工作流一一对应。",
        )

    # (d) 分辨率：用工作流自带的 MarkdownNote 对照表核对，不凭印象
    try:
        data = json.loads(workflow_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        rep.warn(f"工作流 JSON 读不动：{exc}", "确认文件没被改坏（本程序不会改它）。")
        return
    notes = [n for n in data.get("nodes", []) if n.get("type") == "MarkdownNote"]
    table = "\n".join(str((n.get("widgets_values_named") or {}).get("text", "")) for n in notes)
    res = gen["resolution"]
    row = re.search(
        rf"^\|\s*{re.escape(str(res['megapixels']))}\s*\|[^|]*\|\s*(\d+)\s*x\s*(\d+)\s*\|",
        table,
        re.MULTILINE,
    )
    if row and int(row.group(1)) == res["width"] and int(row.group(2)) == res["height"]:
        rep.ok(f"分辨率与工作流对照表一致：megapixels={res['megapixels']} → {res['width']}x{res['height']}")
    elif row:
        rep.error(
            f"分辨率对照表不符：megapixels={res['megapixels']} 表里是 {row.group(1)}x{row.group(2)}，"
            f"配置写的是 {res['width']}x{res['height']}",
            "按工作流对照表修正 director.yaml 的 generation.resolution。",
        )
    else:
        rep.warn(
            f"工作流里查不到 megapixels={res['megapixels']} 对应的分辨率行",
            "人工确认目标分辨率，再更新 director.yaml 与审计报告。",
        )

    # (e) 登记表的 megapixels 默认值是否与配置一致
    reg_mp = (target.get("defaults") or {}).get("megapixels")
    if reg_mp is not None and float(reg_mp) != float(res["megapixels"]):
        rep.error(
            f"登记表 defaults.megapixels={reg_mp} 与 director.yaml 的 {res['megapixels']} 不一致",
            "两边统一（登记表的默认值决定实际生成分辨率）。",
        )
    elif reg_mp is not None:
        rep.ok(f"登记表 defaults.megapixels 与配置一致（{reg_mp}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="导演系统环境自检（只读，阶段 1）")
    parser.add_argument("--no-workflow", action="store_true", help="跳过工作流/节点核对")
    args = parser.parse_args()

    print("导演系统环境自检（只读：不写文件、不提交任务、不读密钥值）")
    print(f"项目根目录：{PROJECT_ROOT}")

    rep = Report()
    check_dirs(rep)
    check_docs(rep)
    conf = check_config(rep)
    check_paths(rep, conf)
    check_secrets(rep, conf)
    if args.no_workflow:
        rep.section("6. 工作流与限值一致性")
        rep.warn("已按要求跳过（--no-workflow）", "提交任何生成任务之前，请至少完整跑一次自检。")
    elif conf:
        rep.section("6. 工作流与限值一致性")
        check_workflow(rep, conf, _load_object_info(rep))
    return rep.summary()


if __name__ == "__main__":
    raise SystemExit(main())
