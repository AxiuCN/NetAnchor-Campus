"""日志配置：按天轮转的文件日志；服务模式不输出到控制台。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOGGER_NAME = "netanchor"
FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_dir: Path | str,
    level: str = "INFO",
    max_files: int = 7,
    console: bool = False,
    filename: str = "service.log",
) -> logging.Logger:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    # 重复调用时先清掉旧 handler，避免日志重复写
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=directory / filename,
        when="midnight",
        backupCount=max_files,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        logger.addHandler(stream)

    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
