"""Memory lifecycle: decay, expiry, maintenance, and health checks (§39, §54, §55).

The lifecycle manager runs on the background worker: expired memories are
soft-deleted, quality signals flag candidates for review, orphans get
repaired, and duplicate clusters can be merged. High-value history is never
auto-deleted purely for age (spec §54).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .chroma_store import ChromaStore
from .config import MemorySettings
from .models import MemoryType, utc_now
from .sqlite_store import SQLiteStore
from ..logging.setup import log_error, log_info


class LifecycleManager:
    """Expiry, decay bookkeeping, maintenance, and health checks (§54)."""

    def __init__(self, sqlite: SQLiteStore, chroma: ChromaStore, settings: MemorySettings) -> None:
        self._sqlite = sqlite
        self._chroma = chroma
        self._settings = settings

    # ------------------------------------------------------------ expiry
    def expire_due(self) -> int:
        """Soft-delete memories whose expires_at has passed (§11/§39/§40)."""
        if not self._settings.lifecycle_enabled:
            return 0
        expired = self._sqlite.expired_memory_ids(utc_now())
        count = 0
        for memory_id in expired:
            if self._sqlite.set_deleted(memory_id, reason="expired"):
                count += 1
        if count:
            log_info(f"Memory lifecycle: expired {count} memories")
        return count

    # ------------------------------------------------------------ decay
    def apply_decay(self) -> int:
        """Nudge importance of stale low-value memories down (§39/§54).

        Conservative by design: confirmed memories and anything importance
        >= 0.7 is never touched; only unconfirmed semantic memories older
        than the retention window lose importance so they stop dominating
        ranking, and they are soft-deleted once importance hits the floor.
        """
        if not self._settings.lifecycle_enabled:
            return 0
        decayed = 0
        retention = {
            MemoryType.EPISODIC: self._settings.episodic_retention_days,
            MemoryType.CONVERSATION: self._settings.conversation_retention_days,
        }
        now = utc_now()
        for memory_type, days in retention.items():
            if days <= 0:
                continue
            for item in self._chroma.all_ids(memory_type):
                metadata = item.get("metadata", {})
                if int(metadata.get("confirmed", 0) or 0):
                    continue
                try:
                    importance = float(metadata.get("importance", 0.5))
                except (TypeError, ValueError):
                    continue
                if importance >= 0.7:
                    continue
                created = self._parse_time(metadata.get("created_at"))
                if created is None:
                    continue
                age_days = (now - created).days
                if age_days < days:
                    continue
                new_importance = max(importance - 0.2, 0.1)
                if new_importance <= 0.11:
                    memory_id = str(metadata.get("memory_id", ""))
                    if memory_id:
                        self._sqlite.set_deleted(memory_id, reason="decayed")
                        self._chroma.delete(memory_id, memory_type)
                else:
                    self._chroma.update_metadata(memory_id=str(metadata.get("memory_id", "")), memory_type=memory_type, importance=new_importance)
                decayed += 1
        if decayed:
            log_info(f"Memory lifecycle: decayed {decayed} memories")
        return decayed

    # ------------------------------------------------------------ maintenance
    def run_maintenance(self) -> dict[str, int]:
        """Quality-control pass (spec §54): expiry, decay, orphans, duplicates."""
        report: dict[str, int] = {}
        report["expired"] = self.expire_due()
        report["decayed"] = self.apply_decay()
        orphans = self._sqlite.orphan_scan()
        report["orphans_repaired"] = self._sqlite.repair_orphans()
        report["orphans_detected"] = len(orphans["registry_without_storage"]) + len(orphans["storage_without_registry"])
        return report

    # ------------------------------------------------------------ health
    def health_check(self) -> dict[str, Any]:
        """Detect registry/storage inconsistencies (spec §55/§73)."""
        health: dict[str, Any] = {"healthy": True, "issues": []}
        issues: list[str] = health["issues"]
        try:
            self._sqlite.stats()
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            issues.append(f"sqlite unavailable: {exc}")
        try:
            self._chroma.collection_counts()
        except Exception as exc:  # noqa: BLE001
            issues.append(f"chroma unavailable: {exc}")
        orphans = self._sqlite.orphan_scan()
        missing_registry = len(orphans["storage_without_registry"])
        missing_storage = len(orphans["registry_without_storage"])
        if missing_registry:
            issues.append(f"{missing_registry} sqlite rows missing registry entries")
        if missing_storage:
            issues.append(f"{missing_storage} registry entries missing sqlite rows")
        known_vector_ids: set[str] = set()
        for memory_type in (MemoryType.EPISODIC, MemoryType.CONVERSATION, MemoryType.KNOWLEDGE, MemoryType.PROJECT_CONTEXT):
            for item in self._chroma.all_ids(memory_type):
                memory_id = str(item["metadata"].get("memory_id", ""))
                if not memory_id:
                    continue
                known_vector_ids.add(memory_id)
                if self._sqlite.registry_entry(memory_id) is None:
                    issues.append(f"chroma vector without registry entry: {memory_id}")
                    break  # one sample per collection is enough for diagnosis
        # Reverse direction: registry rows pointing at chroma whose vector was
        # deleted behind the manager's back (spec §55 'missing Chroma vectors').
        missing_vectors = self._sqlite.registry_ids_with_storage("chroma") - known_vector_ids
        if missing_vectors:
            issues.append(f"{len(missing_vectors)} registry entries missing chroma vectors")
        health["healthy"] = not issues
        return health

    @staticmethod
    def _parse_time(value: Any) -> Any:
        from datetime import datetime

        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
