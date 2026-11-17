import logging
import pathlib

from netanchor.logging_setup import LOGGER_NAME, setup_logging


def test_writes_to_file(tmp_path):
    logger = setup_logging(tmp_path / "logs")
    logger.info("hello")
    target = tmp_path / "logs" / "service.log"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "hello" in content
    assert "[INFO]" in content


def test_repeated_setup_does_not_duplicate_handlers(tmp_path):
    first = setup_logging(tmp_path / "logs")
    setup_logging(tmp_path / "logs")
    setup_logging(tmp_path / "logs")
    assert len(first.handlers) == 1
    assert logging.getLogger(LOGGER_NAME) is first


def test_console_handler_added_when_requested(tmp_path):
    logger = setup_logging(tmp_path / "logs", console=True)
    assert len(logger.handlers) == 2
    setup_logging(tmp_path / "logs", console=False)


def test_level_applied(tmp_path):
    logger = setup_logging(tmp_path / "logs", level="DEBUG")
    assert logger.level == logging.DEBUG
    setup_logging(tmp_path / "logs", level="INFO")

def test_rotation_puts_date_before_extension(tmp_path):
    """轮转后的文件名必须是 service.<日期>.log——.log 要留在最后。"""
    logger = setup_logging(tmp_path / "logs")
    handler = logger.handlers[0]
    base = str(tmp_path / "logs" / "service.log")

    rotated = handler.rotation_filename(base + ".2026-11-14")

    assert rotated == str(tmp_path / "logs" / "service.2026-11-14.log")
    assert rotated.endswith(".log")


def test_old_rotated_files_beyond_backup_count_are_deleted(tmp_path):
    logger = setup_logging(tmp_path / "logs", max_files=2)
    logs = tmp_path / "logs"
    for day in ("01", "02", "03", "04"):
        (logs / f"service.2026-11-{day}.log").write_text("x", encoding="utf-8")

    to_delete = logger.handlers[0].getFilesToDelete()

    assert [pathlib.Path(p).name for p in to_delete] == [
        "service.2026-11-01.log",
        "service.2026-11-02.log",
    ]


def test_live_log_file_is_never_listed_for_deletion(tmp_path):
    logger = setup_logging(tmp_path / "logs", max_files=1)
    logger.info("hello")
    (tmp_path / "logs" / "service.2026-11-01.log").write_text("x", encoding="utf-8")

    to_delete = [pathlib.Path(p).name for p in logger.handlers[0].getFilesToDelete()]

    assert "service.log" not in to_delete
