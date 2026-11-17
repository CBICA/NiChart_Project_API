"""
Content hashes of project files, cached by size and mtime.

Provenance compares what a step read or wrote with what is on disk now, and
publishes checksums so a replication can be checked file by file. Re-hashing a
cohort of NIfTIs on every request would be slow, so each file's SHA-256 is
cached in ``{study_dir}/_working/file_hashes.json`` under its project-relative
path, together with the size and ``mtime_ns`` it was computed for. A file whose
size or mtime has changed is hashed again.

A lost cache write (two writers racing) only costs a re-hash, never a wrong
answer, because an entry is used only while size and mtime still match.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

_CACHE_FILE = "_working/file_hashes.json"

# Never part of a dataset's content: metadata the executor writes next to outputs,
# and partial writes.
_IGNORED_NAMES = {"_provenance.json"}
_IGNORED_SUFFIXES = (".tmp",)

_lock = threading.Lock()


@dataclass(frozen=True)
class FileHash:
    path: str        # project-relative, POSIX separators
    size: int
    sha256: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ignored(p: Path) -> bool:
    return p.name in _IGNORED_NAMES or p.name.endswith(_IGNORED_SUFFIXES)


def list_files(study_dir: Path, rel: str) -> list[Path]:
    """Regular files at project-relative *rel* (a file, or every file under a directory)."""
    root = study_dir / rel
    if root.is_file():
        return [] if _ignored(root) else [root]
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not _ignored(p))


class HashCache:
    """Load once, hash many files, save once."""

    def __init__(self, study_dir: Path) -> None:
        self._study_dir = study_dir
        self._path = study_dir / _CACHE_FILE
        self._entries: dict[str, dict] = {}
        self._dirty = False
        try:
            raw = json.loads(self._path.read_text())
            if isinstance(raw, dict):
                self._entries = raw
        except (OSError, ValueError):
            pass

    def hash(self, file: Path) -> FileHash:
        rel = file.relative_to(self._study_dir).as_posix()
        st = file.stat()
        entry = self._entries.get(rel)
        if entry and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
            return FileHash(rel, st.st_size, entry["sha256"])
        digest = sha256_file(file)
        self._entries[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}
        self._dirty = True
        return FileHash(rel, st.st_size, digest)

    def hash_tree(self, rel: str) -> list[FileHash]:
        out = []
        for f in list_files(self._study_dir, rel):
            try:
                out.append(self.hash(f))
            except OSError:
                continue  # vanished mid-scan
        return out

    def save(self) -> None:
        if not self._dirty:
            return
        with _lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex[:8]}.tmp")
                tmp.write_text(json.dumps(self._entries))
                tmp.replace(self._path)
                self._dirty = False
            except OSError as exc:
                _log.warning("Could not save file hash cache for %s: %s", self._study_dir, exc)


def tree_digest(files: list[FileHash], base: str = "") -> str:
    """One SHA-256 for a set of files: over their ``<sha256>  <path>`` lines, sorted by path.

    Paths are taken relative to *base* (the dataset root), so the same files
    digest the same wherever the dataset sits.
    """
    prefix = base.rstrip("/") + "/" if base else ""
    rel = sorted(
        (f.path[len(prefix):] if prefix and f.path.startswith(prefix) else f.path, f.sha256)
        for f in files
    )
    return hashlib.sha256("".join(f"{sha}  {p}\n" for p, sha in rel).encode()).hexdigest()
