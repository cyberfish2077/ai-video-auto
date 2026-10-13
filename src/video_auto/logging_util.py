"""日志配置（阶段六起共用）。

约定：控制台看得见的进度 + 文件里留得住的历史。日志文件按天切分，
放在 config/settings.yaml 的 paths.logs 目录下。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


def setup_logging(log_dir: Path, name: str, *, level: str = "INFO") -> logging.Logger:
    """建立控制台 + 文件双通道日志，返回可用的 logger。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_file = log_dir / f"{name}-{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def cleanup_logs(log_dir: Path, keep_days: int, *, prefix: str | None = None) -> list[Path]:
    """删除超过保留天数的日志文件，返回被删除的清单。

    只删除本程序按 ``<prefix>-YYYYMMDD.log`` 规则生成的日志，绝不碰其它文件。
    """
    if keep_days <= 0:
        return []
    removed: list[Path] = []
    today = datetime.now().date()
    for path in sorted(log_dir.glob("*.log")):
        if prefix and not path.name.startswith(prefix):
            continue
        parts = path.stem.rsplit("-", 1)
        if len(parts) != 2 or len(parts[1]) != 8 or not parts[1].isdigit():
            continue
        try:
            day = datetime.strptime(parts[1], "%Y%m%d").date()
        except ValueError:
            continue
        if (today - day).days > keep_days:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    return removed
