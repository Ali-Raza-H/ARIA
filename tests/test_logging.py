import logging
from pathlib import Path

from aria.logging_setup import configure_logging, log, log_call


def make_logger(tmp_path: Path):
    configure_logging(tmp_path, max_bytes=65536, backup_count=2)
    return tmp_path


def read(tmp_path: Path, name: str) -> str:
    return (tmp_path / name).read_text(encoding="utf-8")


def test_levels_are_separated_across_files(tmp_path: Path) -> None:
    make_logger(tmp_path)
    log("debug", "only debug sees this")
    log("info", "info and debug see this")
    log("error", "everyone sees this")

    debug = read(tmp_path, "debug.log")
    info = read(tmp_path, "info.log")
    error = read(tmp_path, "error.log")

    assert "only debug sees this" in debug
    assert "only debug sees this" not in info
    assert "info and debug see this" in info
    assert "info and debug see this" not in error
    assert "everyone sees this" in error
    assert "everyone sees this" in info
    assert "everyone sees this" in debug


def test_rotating_backups_created(tmp_path: Path) -> None:
    make_logger(tmp_path)
    # Fill more than one rotation's worth of the debug file.
    for index in range(200):
        log("debug", "x" * 1024 + f" {index}")
    files = sorted(p.name for p in tmp_path.glob("debug.log*"))
    assert "debug.log" in files
    assert any(name.startswith("debug.log.") for name in files), files


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

    debug = read(tmp_path, "debug.log")
    error = read(tmp_path, "error.log")
    assert "call -> " in debug and "add" in debug
    assert "call <- " in debug and "5" in debug
    assert "boom raised ValueError: nope" in error


def test_log_level_filtering_matches_config(tmp_path: Path) -> None:
    make_logger(tmp_path)
    logger = logging.getLogger("aria")
    levels = {handler.level for handler in logger.handlers}
    assert logging.DEBUG in levels
    assert logging.INFO in levels
    assert logging.ERROR in levels
