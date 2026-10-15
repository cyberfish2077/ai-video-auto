"""交付前核对：成片里每一段装的到底是不是台账里那一段。

为什么有这个脚本（2026-10-15 实测事故）：compose_final.py 在直接拼接前会把每段规范化写进临时目录，
文件名原来用"原文件名"命名；而"沿用上一部片的片段"与本片片段可能同名（两边的 s2_00001-audio.mp4），
后写的覆盖先写的 → 成片开头的第 2、3 个镜头被本片同号段顶掉。
**台账与合成记录里路径都写得对**，所以只有比对成片的实际画面才能发现（这次是用户看片抓到的）。

做法：按台账把成片切成 N 个窗口，把每个窗口与 N 个候选源都降成"每秒 fps 帧、固定尺寸灰度缩略图"，
逐字节算平均差（MAD）：窗口最像谁、差值多少，一目了然。全部窗口都对应自己那一段才算通过。

用法::

    .venv\\Scripts\\python.exe scripts\\verify_final_segments.py --project animal-heads-nap-02
    .venv\\Scripts\\python.exe scripts\\verify_final_segments.py --project <片名> --tolerance 3 --json out.json

退出码：0 = 全部窗口都对应自己那一段；1 = 有不一致（打印是哪几个窗口）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from video_auto import config as cfg  # noqa: E402
from video_auto import tasks as task_mod  # noqa: E402


def raster(ffmpeg: Path, path: Path, out: Path, *, ss: float | None = None,
           duration: float | None = None, fps: int = 2, size: str = "16x9") -> bytes:
    """把一个视频（或它的一段）降成 rawvideo 灰度缩略图，返回字节串。"""
    args = [str(ffmpeg), "-y", "-v", "error"]
    if ss is not None:
        args += ["-ss", f"{ss:.4f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{duration:.4f}"]
    args += ["-vf", f"fps={fps},scale={size},format=gray", "-f", "rawvideo", str(out)]
    subprocess.run(args, check=True)
    return out.read_bytes()


def mad(a: bytes, b: bytes) -> float:
    """两个缩略图序列的平均逐字节差（0 = 完全一样）。"""
    n = min(len(a), len(b))
    if n == 0:
        return float("inf")
    return sum(abs(a[i] - b[i]) for i in range(n)) / n


def main() -> int:
    parser = argparse.ArgumentParser(description="核对成片里每一段是否就是台账里那一段")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", help="成片路径（默认 final/<项目名>.mp4）")
    parser.add_argument("--fps", type=int, default=2, help="每秒取几帧做缩略图（默认 2）")
    parser.add_argument("--size", default="16x9", help="缩略图尺寸（默认 16x9）")
    parser.add_argument("--tolerance", type=float, default=5.0,
                        help="判定一致的最大平均字节差（默认 5.0；完全相同应为 0）")
    parser.add_argument("--json", help="把结果另存为 JSON（便于归档进分析报告）")
    args = parser.parse_args()

    settings = cfg.load_settings()
    ffmpeg = settings.path("ffmpeg.ffmpeg")
    project_dir = settings.path("paths.projects") / args.project
    store = task_mod.ProjectStore(project_dir)
    if not store.exists():
        print(f"[错误] 项目不存在：{store.path}")
        return 2
    project = store.load()
    final = Path(args.output) if args.output else project_dir / "final" / f"{args.project}.mp4"
    if not final.is_file():
        print(f"[错误] 找不到成片：{final}")
        print(f"       下一步：先跑 scripts\\compose_final.py --project {args.project}")
        return 2

    segments = [seg for seg in project.segments
                if seg.status in (task_mod.STATUS_DONE, task_mod.STATUS_REUSED) and seg.output_path]
    if not segments:
        print("[错误] 台账里没有可核对的片段（状态需为 done 或 reused）")
        return 2

    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="verify_final_") as tmp:
        tmp_dir = Path(tmp)
        sources: dict[str, bytes] = {}
        missing: list[str] = []
        for seg in segments:
            path = Path(str(seg.output_path))
            if not path.is_file():
                missing.append(f"{seg.id} -> {path}")
                continue
            sources[seg.id] = raster(ffmpeg, path, tmp_dir / f"src_{seg.id}.raw",
                                     fps=args.fps, size=args.size)
        if missing:
            print("[错误] 这些片段的产物文件不存在：")
            for item in missing:
                print(f"        - {item}")
            return 2

        print("=" * 78)
        print(f"成片核对：{final.name}　对照台账 {len(segments)} 段")
        print("=" * 78)
        print(f"{'窗口':10s} {'区间':>16s} {'最像的源':>10s} {'差值':>7s} {'次优':>16s}  结论")
        clock = 0.0
        for seg in segments:
            duration = float(seg.duration_seconds or 0)
            data = raster(ffmpeg, final, tmp_dir / f"win_{seg.id}.raw",
                          ss=clock, duration=duration, fps=args.fps, size=args.size)
            scores = sorted((mad(data, src), sid) for sid, src in sources.items())
            best = scores[0]
            second = scores[1] if len(scores) > 1 else (float("inf"), "-")
            ok = best[1] == seg.id and best[0] <= args.tolerance
            results.append({"segment": seg.id, "start": round(clock, 4), "duration": duration,
                            "best_match": best[1], "best_diff": round(best[0], 3),
                            "second_match": second[1], "second_diff": round(second[0], 3),
                            "ok": ok})
            line = (f"{seg.id:10s} {clock:6.2f}~{clock + duration:6.2f}s "
                    f"{best[1]:>10s} {best[0]:7.2f} {second[1]}({second[0]:.2f})")
            print(line + ("  ✓" if ok else "  ✗ 对不上"))
            clock += duration

    bad = [r for r in results if not r["ok"]]
    print()
    if bad:
        print(f"[失败] {len(bad)} / {len(results)} 个窗口与台账对不上：")
        for item in bad:
            print(f"        - {item['segment']}（{item['start']}~{item['start'] + item['duration']:.2f}s）"
                  f" 最像 {item['best_match']}，差值 {item['best_diff']}")
        print("       下一步：检查合成是否临时文件名撞名/顺序错位；"
              "台账与合成记录里的路径写对不代表成片用对了。")
    else:
        print(f"[通过] {len(results)} 个窗口全部对应台账里自己的那一段。")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"project": args.project, "final": str(final), "tolerance": args.tolerance,
             "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"       结果已写入 {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
