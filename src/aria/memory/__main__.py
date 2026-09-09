"""CLI for memory inspection, maintenance, and backup (spec §42, §54, §57).

Run with ``uv run python -m aria.memory stats|search|recent|wipe|health|
maintenance|backup|ingest``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from .config import MemorySettings
from .manager import MemoryManager
from ..config import MemoryConfig
from ..logging.setup import configure_logging


def _build_manager(root: Path) -> MemoryManager:
    settings = MemorySettings.from_config(MemoryConfig(), root)
    return MemoryManager(root, settings, provider=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARIA memory subsystem CLI")
    parser.add_argument("-c", "--config", type=Path, default=Path("config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Show memory statistics")
    search = sub.add_parser("search", help="Hybrid memory search")
    search.add_argument("query")
    recent = sub.add_parser("recent", help="List recent memories")
    recent.add_argument("-n", "--limit", type=int, default=20)
    health = sub.add_parser("health", help="Registry/storage consistency check")
    maintenance = sub.add_parser("maintenance", help="Run expiry/decay/orphan repair")
    backup = sub.add_parser("backup", help="Back up SQLite + Chroma to a zip")
    backup.add_argument("-o", "--output", type=Path, default=None)
    ingest = sub.add_parser("ingest", help="Ingest a text/markdown/code file")
    ingest.add_argument("path", type=Path)
    wipe = sub.add_parser("wipe", help="Delete ALL memory (destructive)")
    wipe.add_argument("--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args(argv)
    configure_logging(Path("logs"))
    root = Path(".")

    if args.command == "wipe":
        if not args.yes:
            print("This deletes ALL memories. Re-run with --yes to confirm.")
            return 1
        manager = _build_manager(root)
        manager.wipe("all")
        print("All memory wiped.")
        manager.cleanup()
        return 0

    manager = _build_manager(root)
    try:
        if args.command == "stats":
            for key, value in manager.stats().items():
                print(f"{key}: {value}")
        elif args.command == "search":
            results = manager.search(args.query)
            if not results:
                print("No results.")
            for item in results:
                print(f"[{item['score']:.3f}] ({item['type']}) {item['content'][:160]}")
        elif args.command == "recent":
            items = manager.list_recent(limit=args.limit)
            for item in items:
                print(f"{item['created_at'][:19]}  ({item['type']})  {item['content']}")
        elif args.command == "health":
            report = manager.health_check()
            status = "HEALTHY" if report["healthy"] else "ISSUES FOUND"
            print(f"Memory health: {status}")
            for issue in report["issues"]:
                print(f"  - {issue}")
        elif args.command == "maintenance":
            report = manager.run_maintenance()
            for key, value in report.items():
                print(f"{key}: {value}")
        elif args.command == "backup":
            target = backup_stores(manager, args.output)
            print(f"Backup written to {target}")
        elif args.command == "ingest":
            summary = manager.ingest_document(args.path)
            print(summary)
    finally:
        manager.cleanup()
    return 0


def backup_stores(manager: MemoryManager, output: Path | None) -> Path:
    """Zip the SQLite database and Chroma directory (spec §57)."""
    import zipfile

    timestamp = manager.session_id.replace(":", "-")
    target = output or Path(f"memory-backup-{timestamp}.zip")
    with tempfile.TemporaryDirectory() as staging:
        staged_db = Path(staging) / manager.sqlite.path.name
        # SQLite backup API: captures WAL content a plain file copy would miss.
        manager.sqlite.snapshot_to(staged_db)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(staged_db, arcname=f"sqlite/{manager.sqlite.path.name}")
            chroma_dir = manager.settings.chroma_path
            if chroma_dir.exists():
                for file in chroma_dir.rglob("*"):
                    if file.is_file():
                        archive.write(file, arcname=f"chroma/{file.relative_to(chroma_dir)}")
    return target


if __name__ == "__main__":
    sys.exit(main())
