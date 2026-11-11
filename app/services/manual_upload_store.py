"""
Record of manually uploaded intermediate data for a project.

A user who cannot upload raw images may instead supply a later step's inputs by
hand and start a pipeline from that step. Those uploads are recorded here, in
``{study_dir}/_working/manual_uploads.json``, for two purposes:

* **Provenance.** Results derived from hand-supplied intermediates are not
  reproducible from the project's raw data, and the record is the only thing
  that says so. Upload records are append-only.
* **Cache correctness.** A step whose output was replaced by hand did not
  produce what is now on disk, so its cached result must not be reused. The
  check runs against recorded upload paths rather than against cache entries,
  which is what lets it cover entries written before cache records carried
  their output paths.

Provenance is tracked per file, not per slot. Uploading into a folder slot
merges by default, and a later pipeline run that writes to the same folder need
not overwrite every file the user put there, so "the folder was rewritten" says
nothing about whether it is clean. Each upload therefore fingerprints the files
it wrote, and a file counts as manual data for exactly as long as it is still
on disk with that content (``find_manual_data``). A step that reads manual data
has its outputs fingerprinted too (``record_derived``), so data computed from
an upload stays marked until it is regenerated from clean inputs.

Records written before per-file fingerprints existed only name the slot. For
those, every file under the slot not modified since the upload is treated as
possibly uploaded: that over-reports files which pre-dated the upload, but never
misses one that came from it.

Lives apart from ``job_service`` so that the upload path can write records and
the orchestrator can read them without the two importing each other.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_MANUAL_UPLOADS_FILE = "_working/manual_uploads.json"
_HASH_CHUNK = 1024 * 1024


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a uniquely-named temp file + rename.

    Same reasoning as ``job_service._atomic_write_text``: a plain write
    truncates the target first, and every file under a synced project directory
    may be read by an S3 upload mid-write. ``.tmp`` is excluded from S3 sync.
    """
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _path(study_dir: Path) -> Path:
    return study_dir / _MANUAL_UPLOADS_FILE


def _load_raw(study_dir: Path) -> dict:
    """The whole log, ``{"uploads": [...], "derived": [...]}``; empty when unreadable.

    An unreadable log must not take a project's runs down with it.
    """
    p = _path(study_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _dict_list(data: dict, key: str) -> list[dict]:
    items = data.get(key)
    return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []


def _save_raw(study_dir: Path, data: dict) -> None:
    p = _path(study_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, json.dumps(data, indent=2, default=str))


def load_records(study_dir: Path) -> list[dict]:
    """All recorded manual uploads for a project, oldest first."""
    return _dict_list(_load_raw(study_dir), "uploads")


def _paths_overlap(a: Path, b: Path) -> bool:
    """Same rule as ``job_service._paths_overlap``: equal, or one contains the other."""
    return a == b or a in b.parents or b in a.parents


# ── Fingerprints ──────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_files(study_dir: Path, files: list[Path]) -> list[dict]:
    """``{path, size, mtime_ns, sha256}`` for each existing regular file in *files*.

    ``path`` is project-relative. Files that vanished or are not regular files
    are left out rather than failing the caller.
    """
    result = []
    for f in sorted(set(files)):
        try:
            if f.is_symlink() or not f.is_file():
                continue
            st = f.stat()
            result.append({
                "path": f.relative_to(study_dir).as_posix(),
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "sha256": _sha256(f),
            })
        except (OSError, ValueError):
            continue
    return result


def _files_under(paths: list[Path]) -> list[Path]:
    """Every regular file at or below each of *paths*."""
    out: list[Path] = []
    for p in paths:
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(f for f in p.rglob("*") if f.is_file() and not f.is_symlink())
    return out


class _HashCache:
    """Memoises file hashes for the duration of one lookup."""

    def __init__(self) -> None:
        self._hashes: dict[Path, str | None] = {}

    def sha256(self, path: Path) -> str | None:
        if path not in self._hashes:
            try:
                self._hashes[path] = _sha256(path)
            except OSError:
                self._hashes[path] = None
        return self._hashes[path]


def _still_matches(study_dir: Path, fp: dict, hashes: _HashCache) -> bool:
    """True while the fingerprinted file is on disk with the content it had.

    Size and mtime decide without reading the file when they can: a different
    size means rewritten, an identical size and mtime means untouched. Only a
    changed mtime with an unchanged size (a rewrite, or a sync that touched the
    file without changing it) needs the hash.
    """
    rel = fp.get("path")
    if not isinstance(rel, str) or not rel:
        return False
    p = study_dir / rel
    try:
        if p.is_symlink() or not p.is_file():
            return False
        st = p.stat()
    except OSError:
        return False
    if st.st_size != fp.get("size"):
        return False
    if st.st_mtime_ns == fp.get("mtime_ns"):
        return True
    return hashes.sha256(p) == fp.get("sha256")


# ── Recording ─────────────────────────────────────────────────────────────────

def upload_key(record: dict) -> str:
    """Stable identity of an upload record.

    Records written before ``upload_id`` existed are identified by their upload
    time and slot path, which together are unique in an append-only log.
    """
    uid = record.get("upload_id")
    if isinstance(uid, str) and uid:
        return uid
    return f"legacy:{record.get('uploaded_at')}|{record.get('path')}"


def append_record(study_dir: Path, record: dict, uploaded_files: list[Path] | None = None) -> dict:
    """Append one upload record and return it with its id and timestamps filled in.

    *uploaded_files* are the absolute paths the upload wrote; they are
    fingerprinted here, after they are in place, so the recorded mtimes are the
    ones a later lookup will see.
    """
    now = datetime.now(timezone.utc)
    entry = {
        **record,
        "upload_id": uuid.uuid4().hex,
        "uploaded_at": now.isoformat(),
        "uploaded_at_ts": now.timestamp(),
    }
    if uploaded_files is not None:
        entry["uploaded_files"] = fingerprint_files(study_dir, uploaded_files)
    data = _load_raw(study_dir)
    data["uploads"] = [*_dict_list(data, "uploads"), entry]
    _save_raw(study_dir, data)
    return entry


def record_derived(
    study_dir: Path,
    run_id: str,
    step_id: str,
    sources: list[str],
    output_paths: list[Path],
) -> None:
    """Mark the outputs of a step that read manual data as derived from it.

    *sources* are the upload keys the step read. Every file under the step's
    outputs is fingerprinted, including files the step may not have written
    itself: the folder is what downstream steps read, and over-marking is the
    safe direction. Entries none of whose files survive unchanged in size are
    dropped on the way, so the log does not grow without bound.
    """
    fingerprints = fingerprint_files(study_dir, _files_under(output_paths))
    if not fingerprints or not sources:
        return
    data = _load_raw(study_dir)
    kept = [d for d in _dict_list(data, "derived") if _possibly_live(study_dir, d)]
    kept.append({
        "run_id": run_id,
        "step_id": step_id,
        "sources": sorted(set(sources)),
        "files": fingerprints,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    data["derived"] = kept
    _save_raw(study_dir, data)


def _possibly_live(study_dir: Path, derived: dict) -> bool:
    """Cheap check: does any of the entry's files still exist at its recorded size?"""
    for fp in _dict_list(derived, "files"):
        try:
            if (study_dir / fp["path"]).stat().st_size == fp.get("size"):
                return True
        except (OSError, KeyError, TypeError):
            continue
    return False


# ── Lookup ────────────────────────────────────────────────────────────────────

@dataclass
class ManualHit:
    """Manual data from one upload found under a set of paths."""

    files: set[str] = field(default_factory=set)   # project-relative files read
    # True when every file was reached only through the outputs of an earlier
    # step that had read the upload, rather than the uploaded files themselves.
    indirect: bool = True


def find_manual_data(study_dir: Path, paths: list[Path]) -> dict[str, ManualHit]:
    """Hand-supplied data currently on disk under any of *paths*, by upload key.

    A file counts while it still has the content it was uploaded (or derived)
    with; a pipeline overwriting it clears it, but a pipeline merely writing
    elsewhere in the same folder does not. Empty when nothing qualifies.
    """
    data = _load_raw(study_dir)
    uploads = _dict_list(data, "uploads")
    if not uploads:
        return {}
    hashes = _HashCache()
    hits: dict[str, ManualHit] = {}

    def _hit(key: str, rel: str, indirect: bool) -> None:
        h = hits.setdefault(key, ManualHit())
        h.files.add(rel)
        h.indirect = h.indirect and indirect

    def _overlaps(rel: str) -> bool:
        f = study_dir / rel
        return any(_paths_overlap(f, p) for p in paths)

    for record in uploads:
        key = upload_key(record)
        fingerprints = record.get("uploaded_files")
        if isinstance(fingerprints, list):
            for fp in fingerprints:
                if isinstance(fp, dict) and _overlaps(str(fp.get("path", ""))) \
                        and _still_matches(study_dir, fp, hashes):
                    _hit(key, fp["path"], indirect=False)
        else:
            for rel in _legacy_files(study_dir, record, paths):
                _hit(key, rel, indirect=False)

    for derived in _dict_list(data, "derived"):
        sources = [s for s in derived.get("sources") or [] if isinstance(s, str)]
        if not sources:
            continue
        for fp in _dict_list(derived, "files"):
            if _overlaps(str(fp.get("path", ""))) and _still_matches(study_dir, fp, hashes):
                for key in sources:
                    _hit(key, fp["path"], indirect=True)

    return hits


def _legacy_files(study_dir: Path, record: dict, paths: list[Path]) -> list[str]:
    """Files under a pre-fingerprint record's slot that may still be its upload.

    Only the slot is known, so any file under it (and under *paths*) whose
    mtime is no later than the upload is kept. Files written afterwards were
    rewritten by something else — a pipeline, or a later upload with a record
    of its own.
    """
    rel = record.get("path")
    try:
        uploaded_at = float(record.get("uploaded_at_ts"))
    except (TypeError, ValueError):
        return []
    if not isinstance(rel, str) or not rel:
        return []
    slot = study_dir / rel
    # Search the narrower of slot and path wherever the two overlap.
    roots = [
        p if (p == slot or slot in p.parents) else slot
        for p in paths if _paths_overlap(slot, p)
    ]
    found = []
    for f in _files_under(roots):
        try:
            if f.stat().st_mtime <= uploaded_at:
                found.append(f.relative_to(study_dir).as_posix())
        except (OSError, ValueError):
            continue
    return found


def override_paths(study_dir: Path) -> list[tuple[Path, float]]:
    """``(absolute_path, uploaded_at)`` for every recorded upload.

    Consumed by the cache verdict. A record whose path or timestamp is missing
    or malformed is skipped rather than guessed at — it would otherwise either
    invalidate everything or nothing, both worse than ignoring one bad row.
    """
    result: list[tuple[Path, float]] = []
    for record in load_records(study_dir):
        rel = record.get("path")
        ts = record.get("uploaded_at_ts")
        if not isinstance(rel, str) or not rel:
            continue
        try:
            result.append((study_dir / rel, float(ts)))
        except (TypeError, ValueError):
            continue
    return result


def response_record(study_dir: Path, key: str) -> dict | None:
    """The upload record for *key*, trimmed to the fields a run detail exposes."""
    for r in load_records(study_dir):
        if upload_key(r) == key:
            return {
                "upload_key": key,
                "slot_id": r.get("slot_id"),
                "path": r.get("path"),
                "pipeline_id": r.get("pipeline_id"),
                "produced_by_step": r.get("produced_by_step"),
                "files": r.get("files"),
                "total_bytes": r.get("total_bytes"),
                "replace": r.get("replace"),
                "uploaded_at": r.get("uploaded_at"),
            }
    return None
