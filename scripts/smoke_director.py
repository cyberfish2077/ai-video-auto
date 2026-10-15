"""导演系统冒烟自检：**不花钱、不调模型**，把每个脚本与每段拼装/校验都跑一遍。

为什么需要（2026-10-15 教训）：上一轮流程改造（3 闸门 + 先叙事后技术 + 事实单一来源）改完之后
没有端到端跑过——单测全过，真流程一跑就连撞三处 NameError／TypeError：
分镜提示词拼装把概念字典当秒数、剧本评审引用了不存在的变量、剧本返修里 `nominal` 未定义。
**改完流程先跑这一条**，这类"一跑就崩"的问题立刻暴露。

用法::

    .venv\\Scripts\\python.exe scripts\\smoke_director.py
    .venv\\Scripts\\python.exe scripts\\smoke_director.py --film animal-heads-nap-02

退出码：0 = 全部通过；1 = 有失败项（逐条打印）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIRECTOR_ROOT = PROJECT_ROOT / "director_system"
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(DIRECTOR_ROOT / "scripts"))

from video_auto import config as cfg  # noqa: E402

import make_ideas  # noqa: E402
import make_prompts  # noqa: E402
import make_story  # noqa: E402
import make_storyboard  # noqa: E402

PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def cli_help_smoke() -> list[str]:
    """每个导演脚本跑一次 --help：能挡住"导入即崩/参数定义写坏"这类问题。"""
    failures: list[str] = []
    for script in sorted((DIRECTOR_ROOT / "scripts").glob("*.py")):
        proc = subprocess.run([str(PYTHON), str(script), "--help"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            failures.append(f"{script.name} --help 退出码 {proc.returncode}：{(proc.stderr or '')[:200]}")
    return failures


def prompt_and_check_smoke(film_dir: Path) -> list[str]:
    """对一部真实影片：把每段提示词都拼一遍 + 把每个机械校验都跑一遍（都不调模型）。"""
    failures: list[str] = []
    conf = cfg.load_director_config()
    brief = json.loads((film_dir / "brief.json").read_text(encoding="utf-8"))
    ideas = json.loads((film_dir / "ideas.json").read_text(encoding="utf-8"))
    concept_id = (json.loads((film_dir / "story_outline.json").read_text(encoding="utf-8"))
                  .get("concept_id"))
    concept = make_story.find_concept(ideas, concept_id)
    outline = json.loads((film_dir / "story_outline.json").read_text(encoding="utf-8"))
    screenplay = json.loads((film_dir / "screenplay.json").read_text(encoding="utf-8"))
    review = json.loads((film_dir / "screenplay_review.json").read_text(encoding="utf-8"))
    board = json.loads((film_dir / "storyboard.json").read_text(encoding="utf-8"))
    board_review = json.loads((film_dir / "storyboard_review.json").read_text(encoding="utf-8"))
    pack = json.loads((film_dir / "prompt_pack.json").read_text(encoding="utf-8"))
    bible = screenplay.get("continuity_bible") or {}
    nominal = make_story.nominal_seconds_of(brief, conf)
    schemas = {name: make_ideas.load_schema(name) for name in (
        "idea_proposal.schema.json", "idea_review.schema.json", "story_outline.schema.json",
        "screenplay.schema.json", "storyboard.schema.json", "prompt_pack.schema.json",
        "artifact_review.schema.json")}

    def build(label: str, func) -> None:
        try:
            text = func()
            if not isinstance(text, str) or len(text) < 200:
                failures.append(f"{label}：拼出来的文本过短（{len(text) if isinstance(text, str) else type(text)}）")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}：{type(exc).__name__}: {exc}")

    build("创意提案提示词", lambda: make_ideas.ideas_user_prompt(brief, conf, schemas["idea_proposal.schema.json"]))
    build("创意评审提示词", lambda: make_ideas.review_user_prompt(brief, ideas, conf, schemas["idea_review.schema.json"]))
    build("大纲提示词", lambda: make_story.outline_user_prompt(brief, concept, [], conf, schemas["story_outline.schema.json"]))
    build("剧本提示词", lambda: make_story.screenplay_user_prompt(
        brief, concept, outline, [], conf, schemas["screenplay.schema.json"]))
    build("剧本评审提示词", lambda: make_story.review_user_prompt(
        screenplay, conf, concept, schemas["artifact_review.schema.json"], brief=brief))
    build("大纲返修提示词", lambda: make_story.outline_revision_prompt(
        brief, concept, conf, outline, ["示例必修项"], "", 2, schemas["story_outline.schema.json"]))
    build("剧本返修提示词", lambda: make_story.screenplay_revision_prompt(
        brief, concept, conf, outline, screenplay, ["示例必修项"], "", 2, schemas["screenplay.schema.json"]))
    build("分镜提示词", lambda: make_storyboard.storyboard_user_prompt(
        brief, concept, screenplay, review, "", conf, schemas["storyboard.schema.json"]))
    build("分镜评审提示词", lambda: make_storyboard.review_user_prompt(
        board, conf, concept, schemas["artifact_review.schema.json"], brief))
    build("分镜返修提示词", lambda: make_storyboard.revision_user_prompt(
        brief, concept, board, board_review, ["示例必修项"], 2, conf, schemas["storyboard.schema.json"]))
    build("提示词编译提示词", lambda: make_prompts.compile_user_prompt(
        brief, board, board_review, "", "示例风格前缀", "photoreal-cinematic", conf,
        schemas["prompt_pack.schema.json"]))
    build("提示词评审提示词", lambda: make_prompts.review_user_prompt(
        pack, conf, board, schemas["artifact_review.schema.json"]))

    # 每个机械校验都跑一遍（用真的产物，能挡住"校验函数自己在某条路径上崩")
    checks = {
        "大纲时长": lambda: make_story.duration_problems(outline, nominal, conf),
        "剧本时长": lambda: make_story.duration_problems(screenplay, nominal, conf),
        "剧本返修校验（显式函数）": lambda: make_story.revision_checks(
            screenplay, nominal, conf, len(make_story.must_fix_texts(review))),
        "分镜规则": lambda: make_storyboard.storyboard_problems(board, conf, nominal, bible),
        "分镜返修校验（显式函数）": lambda: make_storyboard.revision_board_checks(
            board, nominal, conf, bible, len(make_story.must_fix_texts(board_review))),
        "提示词包": lambda: make_prompts.prompt_problems(
            {**pack, "_style_prefix": pack["shots"][0]["prompt_lines"]["style_line"][:12]}, board, conf),
        "角色表泄漏": lambda: make_prompts.character_bible_leak_problems(bible),
    }
    for label, func in checks.items():
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}：{type(exc).__name__}: {exc}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="导演系统冒烟自检（不调模型、不花钱）")
    parser.add_argument("--film", help="用哪部影片的真实产物做冒烟（默认取最近改动的一部）")
    args = parser.parse_args()

    settings = cfg.load_settings()
    films_root = settings.path("paths.projects")
    if args.film:
        film_dir = films_root / args.film
    else:
        candidates = [p for p in films_root.glob("*/brief.json")
                      if (p.parent / "prompt_pack.json").is_file()]
        if not candidates:
            print("[错误] 找不到同时有 brief.json 与 prompt_pack.json 的影片目录。")
            return 2
        film_dir = max(candidates, key=lambda p: p.stat().st_mtime).parent
    if not (film_dir / "brief.json").is_file():
        print(f"[错误] 影片目录不完整：{film_dir}")
        return 2

    print("=" * 78)
    print(f"导演系统冒烟自检　影片：{film_dir.name}　（不调模型）")
    print("=" * 78)
    failures = cli_help_smoke()
    print(f"  [1/2] 各脚本 --help：{'通过' if not failures else str(len(failures)) + ' 处失败'}")
    second = prompt_and_check_smoke(film_dir)
    failures += second
    print(f"  [2/2] 提示词拼装 + 机械校验：{'通过' if not second else str(len(second)) + ' 处失败'}")
    if failures:
        print()
        print("[失败] 逐条如下：")
        for item in failures:
            print(f"        - {item}")
        print("       下一步：修掉上面每条之后重跑本命令；这类问题**不要**等到调模型时才发现。")
        return 1
    print()
    print("[通过] 所有脚本可导入、所有提示词可拼装、所有机械校验可执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
