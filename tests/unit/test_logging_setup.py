import logging

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
