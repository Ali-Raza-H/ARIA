"""Document ingestion into knowledge memory (spec §64/§65).

Pipeline: file → parse (text/markdown/code) → clean → chunk (paragraph-based
with configurable target size) → embed → Chroma ``knowledge`` collection, with
registry entries and source traceability (filename, document_id, chunk_id).
PDF/HTML parsers can be added later behind the same entry point.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import MemoryType, SemanticMemory
from ..sqlite.store import content_hash, new_id
from ...logging.setup import log_error, log_info

if TYPE_CHECKING:
    from ..manager import MemoryManager

# Extensions accepted by the text/code parser (spec §65 scope for v1).
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".ts", ".tsx", ".js", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".sql", ".css",
    ".html", ".xml", ".csv", ".log",
}

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB safety cap


def clean_text(text: str) -> str:
    """Normalize whitespace while keeping paragraph structure (§64)."""
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def chunk_text(text: str, target_chars: int = 1200, overlap_chars: int = 150) -> list[str]:
    """Paragraph-based chunking with configurable size and overlap (§64)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if current_len + len(paragraph) > target_chars and current:
            chunks.append("\n\n".join(current))
            # Overlap: carry the tail of the previous chunk into the next one.
            tail = chunks[-1][-overlap_chars:]
            current = [tail, paragraph] if overlap_chars > 0 else [paragraph]
            current_len = len(tail) + len(paragraph)
        else:
            current.append(paragraph)
            current_len += len(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def ingest_file(manager: "MemoryManager", path: Path, *, user_id: str = "default", topic: str = "") -> dict[str, Any]:
    """Ingest one file into knowledge memory; returns a summary (§65)."""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        return {"chunks": 0, "error": f"not a file: {file_path}"}
    if file_path.stat().st_size > MAX_FILE_BYTES:
        return {"chunks": 0, "error": f"file too large (> {MAX_FILE_BYTES // (1024 * 1024)} MiB): {file_path.name}"}
    if file_path.suffix.lower() not in TEXT_EXTENSIONS:
        return {"chunks": 0, "error": f"unsupported file type: {file_path.suffix or '(none)'}"}
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"chunks": 0, "error": f"could not read {file_path.name}: {exc}"}

    text = clean_text(raw)
    if not text:
        return {"chunks": 0, "error": "file is empty after cleaning"}
    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    chunks = chunk_text(text)
    stored = 0
    for index, chunk in enumerate(chunks):
        memory = SemanticMemory(
            id=new_id(MemoryType.KNOWLEDGE),
            user_id=user_id,
            type=MemoryType.KNOWLEDGE,
            content=chunk,
            importance=0.6,
            confidence=0.9,
            source="document",
            source_reference=f"{document_id}:{file_path.name}",
            confirmed=True,
            topic=topic or file_path.stem,
        )
        chroma_id = manager.chroma.add(memory)
        manager.sqlite.register_semantic(
            memory.id, user_id, MemoryType.KNOWLEDGE, chroma_id,
            importance=memory.importance, confidence=memory.confidence,
            hash_value=content_hash(chunk),
        )
        stored += 1
    log_info(f"Memory ingestion: {file_path.name} → {stored} chunk(s) as {document_id}")
    return {"chunks": stored, "document_id": document_id, "file": str(file_path)}
