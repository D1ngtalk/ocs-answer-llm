# -*- coding: utf-8 -*-
"""日志初始化：控制台 + 可选文件。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    logger = logging.getLogger("ocs")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s"))
            logger.addHandler(handler)
        except OSError as exc:  # 日志文件写不了不影响主流程
            logger.warning("无法写入日志文件 %s：%s", log_file, exc)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
