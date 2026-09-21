"""结构化日志。

- 日志统一写入 logs/app.log（RotatingFileHandler，2MB×3，UTF-8）。
- log_event 输出单行结构化文本：event=xxx k=v ...
- 控制台 handler 仅在 CNKI_HOVER_DEBUG=1 时挂载。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .paths import LOG_DIR, ensure_dirs

_LOG_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_cnki_configured", False):
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    ensure_dirs()

    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_LOG_FORMAT)
    logger.addHandler(fh)

    if os.environ.get("CNKI_HOVER_DEBUG") == "1":
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(_LOG_FORMAT)
        logger.addHandler(ch)

    logger._cnki_configured = True  # type: ignore[attr-defined]
    return logger


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    """记录一条结构化事件，字段以 k=v 追加。"""
    parts = [f"event={event}"]
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    logger.info(" ".join(parts))
