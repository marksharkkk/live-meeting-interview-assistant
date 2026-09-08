import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import List

from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader, TextLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from .config import settings
except ImportError:
    from config import settings


logger = logging.getLogger(__name__)


class KnowledgeBase:
    def __init__(self, kb_dir: Path | str | None = None):
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
        )
        self.kb_dir = Path(kb_dir or settings.knowledge_base_dir).resolve()
        self.uploads_dir = self.kb_dir / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self._documents: List[Document] = []
        self._cache_file = self.kb_dir / "kb_cache.json"
        self._lock = threading.RLock()
        if not self._load_cache():
            self._rebuild_from_uploads()

    def _load_cache(self) -> bool:
        if not self._cache_file.exists():
            return False
        try:
            raw = json.loads(self._cache_file.read_text(encoding="utf-8"))
            entries = raw.get("documents", [])
            if not isinstance(entries, list):
                raise ValueError("documents must be a list")
            migrated = False
            documents = []
            for item in entries:
                if not isinstance(item, dict) or "page_content" not in item:
                    continue
                metadata = dict(item.get("metadata") or {})
                source = str(metadata.get("source", ""))
                if source and not source.startswith("manual:"):
                    normalized = self._relative_source_from_value(source)
                    if normalized != source:
                        metadata["source"] = normalized
                        migrated = True
                documents.append(Document(page_content=str(item["page_content"]), metadata=metadata))
            self._documents = documents
            if migrated:
                self._save_cache()
            logger.info("Loaded %s document chunks from JSON cache", len(self._documents))
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid knowledge-base cache: %s", exc)
            return False

    def _save_cache(self) -> None:
        payload = {
            "version": 1,
            "documents": [
                {
                    "page_content": document.page_content,
                    "metadata": document.metadata,
                }
                for document in self._documents
            ],
        }
        temp_file = self._cache_file.with_suffix(".json.tmp")
        try:
            temp_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(temp_file, self._cache_file)
        finally:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)

    def _load_and_split(self, path: Path) -> list[Document]:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            loader = PyPDFLoader(str(path))
        elif suffix == ".docx":
            loader = Docx2txtLoader(str(path))
        elif suffix == ".txt":
            loader = TextLoader(str(path), encoding="utf-8")
        else:
            raise ValueError("Unsupported document type")

        source = self._relative_source(path)
        source_hash = self._file_hash(path)
        loaded = loader.load()
        for document in loaded:
            document.metadata = {**document.metadata, "source": source, "source_hash": source_hash}
        return self.text_splitter.split_documents(loaded)

    def _relative_source(self, path: Path) -> str:
        return f"uploads/{path.name}"

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _relative_source_from_value(self, source: str) -> str:
        normalized = source.replace("\\", "/")
        if normalized.startswith("uploads/"):
            return normalized
        return f"uploads/{Path(normalized).name}"

    def _source_key(self, source: str) -> str:
        if source.startswith("manual:"):
            return source.casefold()
        return self._relative_source_from_value(source).casefold()

    def _rebuild_from_uploads(self) -> None:
        paths = [
            path for path in self.uploads_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".pdf", ".txt", ".docx"}
        ]
        if paths:
            logger.info("Rebuilding knowledge-base cache from %s uploaded files", len(paths))
            self._add_documents_sync([str(path) for path in paths])
        else:
            self._save_cache()

    def _remove_source_locked(self, source: str) -> int:
        normalized = self._source_key(source)
        original_count = len(self._documents)
        self._documents = [
            document
            for document in self._documents
            if self._source_key(str(document.metadata.get("source", ""))) != normalized
        ]
        return original_count - len(self._documents)

    def _add_documents_sync(self, file_paths: list[str]) -> dict:
        loaded_files = []
        with self._lock:
            for file_path in file_paths:
                path = Path(file_path).resolve()
                try:
                    if not path.is_file():
                        raise FileNotFoundError("File not found")
                    chunks = self._load_and_split(path)
                    self._remove_source_locked(str(path))
                    self._documents.extend(chunks)
                    loaded_files.append({
                        "file": path.name,
                        "status": "success",
                        "chunks": len(chunks),
                    })
                except Exception as exc:
                    logger.error("Error loading %s: %s", path, exc)
                    loaded_files.append({
                        "file": path.name,
                        "status": "failed",
                        "reason": str(exc),
                    })
            self._save_cache()

        success_count = sum(item["status"] == "success" for item in loaded_files)
        total_chunks = sum(item.get("chunks", 0) for item in loaded_files)
        return {
            "success": success_count > 0,
            "message": f"成功加载 {success_count} 个文件，共 {total_chunks} 个文本块",
            "files": loaded_files,
        }

    async def add_documents(self, file_paths: list[str]) -> dict:
        return await asyncio.to_thread(self._add_documents_sync, file_paths)

    @staticmethod
    def _keywords(question: str) -> set[str]:
        lowered = question.casefold()
        keywords = {
            word for word in re.findall(r"[a-z][a-z0-9_-]+", lowered)
            if len(word) >= 2
        }
        for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
            if len(run) <= 2:
                keywords.add(run)
                continue
            keywords.add(run)
            for size in (2, 3):
                keywords.update(run[index:index + size] for index in range(len(run) - size + 1))
        return keywords

    async def query(self, question: str, top_k: int = 3, max_chars: int = 6000) -> str:
        keywords = self._keywords(question)
        if not keywords:
            return ""

        with self._lock:
            scored: list[tuple[int, int]] = []
            for index, document in enumerate(self._documents):
                content = document.page_content.casefold()
                source = str(document.metadata.get("source", "")).casefold()
                score = sum(content.count(keyword) * min(len(keyword), 8) for keyword in keywords)
                if any(keyword in source for keyword in keywords):
                    score += 50
                if score:
                    scored.append((score, index))
            scored.sort(reverse=True)

            result_parts = []
            result_length = 0
            for _score, index in scored[:top_k]:
                content = self._documents[index].page_content.strip()
                remaining = max_chars - result_length
                if remaining <= 0:
                    break
                result_parts.append(content[:remaining])
                result_length += len(result_parts[-1])
            return "\n\n".join(result_parts)

    async def add_text(self, text: str, metadata: dict | None = None) -> bool:
        source = f"manual:{uuid.uuid4()}"
        document = Document(
            page_content=text,
            metadata={**(metadata or {}), "source": source, "kind": "manual"},
        )
        chunks = self.text_splitter.split_documents([document])
        with self._lock:
            self._documents.extend(chunks)
            self._save_cache()
        return True

    def status(self) -> dict:
        with self._lock:
            loaded_sources = {}
            for document in self._documents:
                source = str(document.metadata.get("source", ""))
                if source and not source.startswith("manual:"):
                    loaded_sources[self._source_key(source)] = document.metadata.get("source_hash")
            files = []
            for path in self.uploads_dir.iterdir():
                if path.is_file() and path.suffix.lower() in {".pdf", ".txt", ".docx"}:
                    files.append({
                        "name": path.name,
                        "size": path.stat().st_size,
                        "modified": path.stat().st_mtime,
                        "loaded": (
                            loaded_sources.get(self._source_key(self._relative_source(path)))
                            == self._file_hash(path)
                        ),
                    })
            return {
                "document_count": len(self._documents),
                "files": sorted(files, key=lambda item: item["modified"], reverse=True),
            }

    def loaded_source_names(self) -> set[str]:
        with self._lock:
            names = set()
            for document in self._documents:
                source = str(document.metadata.get("source", ""))
                if not source or source.startswith("manual:"):
                    continue
                name = Path(source.replace("\\", "/")).name
                path = self.uploads_dir / name
                if path.is_file() and document.metadata.get("source_hash") == self._file_hash(path):
                    names.add(name.casefold())
            return names

    def delete_source(self, path: Path) -> int:
        with self._lock:
            removed = self._remove_source_locked(str(path))
            self._save_cache()
            return removed

    def cleanup_missing_sources(self) -> list[str]:
        existing = {
            self._source_key(self._relative_source(path))
            for path in self.uploads_dir.iterdir()
            if path.is_file()
        }
        with self._lock:
            removed_sources = set()
            remaining = []
            for document in self._documents:
                source = str(document.metadata.get("source", ""))
                if source.startswith("manual:") or not source:
                    remaining.append(document)
                elif self._source_key(source) in existing:
                    remaining.append(document)
                else:
                    removed_sources.add(Path(source.replace("\\", "/")).name)
            self._documents = remaining
            self._save_cache()
            return sorted(removed_sources)

    async def clear(self, remove_uploads: bool = True) -> bool:
        with self._lock:
            self._documents = []
            if remove_uploads:
                for path in self.uploads_dir.iterdir():
                    if path.is_file():
                        path.unlink()
            self._save_cache()
        return True
