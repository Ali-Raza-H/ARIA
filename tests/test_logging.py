import logging
from pathlib import Path

from aria.logging_setup import configure_logging, log, log_call


def make_logger(tmp_path: Path):
    configure_logging(tmp_path, max_bytes=65536, backup_count=2)
    return tmp_path


def read(tmp_path: Path, name: str) -> str:
    return (tmp_path / name).read_text(encoding="utf-8")


def test_all_levels_share_one_log_file(tmp_path: Path) -> None:
    make_logger(tmp_path)
    log("debug", "only debug sees this")
    log("info", "info and debug see this")
    log("error", "everyone sees this")

    log_file = read(tmp_path, "aria.log")

    assert "only debug sees this" in log_file
    assert "info and debug see this" in log_file
    assert "everyone sees this" in log_file
    assert sorted(path.name for path in tmp_path.iterdir()) == ["aria.log"]


def test_rotating_backups_created(tmp_path: Path) -> None:
    make_logger(tmp_path)
    # Fill more than one rotation's worth of the debug file.
    for index in range(200):
        log("debug", "x" * 1024 + f" {index}")
    files = sorted(p.name for p in tmp_path.glob("aria.log*"))
    assert "aria.log" in files
    assert any(name.startswith("aria.log.") for name in files), files


def test_log_call_traces_entry_exit_and_errors(tmp_path: Path) -> None:
    make_logger(tmp_path)

    @log_call
    def add(a: int, b: int) -> int:
        return a + b

    @log_call
    def boom() -> None:
        raise ValueError("nope")

    assert add(2, 3) == 5
    try:
        boom()
    except ValueError:
        pass

    log_file = read(tmp_path, "aria.log")
    assert "call -> " in log_file and "add" in log_file
    assert "call <- " in log_file and "5" in log_file
    assert "boom raised ValueError: nope" in log_file


def test_log_level_filtering_matches_config(tmp_path: Path) -> None:
    make_logger(tmp_path)
    logger = logging.getLogger("aria")
    file_handlers = [handler for handler in logger.handlers if isinstance(handler, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].level == logging.DEBUG
