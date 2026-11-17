"""日志配置：按天轮转的文件日志；服务模式不输出到控制台。"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOGGER_NAME = "netanchor"
FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


class _DatedFileHandler(logging.handlers.TimedRotatingFileHandler):
    """按天轮转，并把日期插在扩展名之前。

    默认行为是 `service.log.2026-11-14`，`.log` 不再是最后一个扩展名，
    双击时系统不把它当文本文件。改成 `service.2026-11-14.log`。
    """

    def rotation_filename(self, default_name: str) -> str:
        stem, ext = os.path.splitext(self.baseFilename)
        suffix = default_name[len(self.baseFilename):]
        return f"{stem}{suffix}{ext}"

    def getFilesToDelete(self):  # noqa: N802 - 覆盖标准库方法
        directory, base = os.path.split(self.baseFilename)
        stem, ext = os.path.splitext(base)
        prefix = f"{stem}."
        names = sorted(
            name
            for name in os.listdir(directory)
            if name != base and name.startswith(prefix) and name.endswith(ext)
        )
        if self.backupCount <= 0 or len(names) < self.backupCount:
            return []
        return [os.path.join(directory, name) for name in names[: len(names) - self.backupCount]]


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

    file_handler = _DatedFileHandler(
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
