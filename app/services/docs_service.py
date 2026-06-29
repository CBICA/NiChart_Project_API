"""
Documentation service — loads topic manifests from resources/docs/ and resolves
file paths for serving.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from app.models.docs import DocManifest, DocSection, DocTopicSummary
from app.services.path_security import PathEscapeError, assert_safe_path

_DOCS_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_docs_id(docs_id: str) -> None:
    if not _DOCS_ID_RE.match(docs_id):
        raise HTTPException(400, f"Invalid docs topic identifier: {docs_id!r}")


def _load_manifest_raw(docs_path: Path, docs_id: str) -> dict[str, Any]:
    manifest_path = docs_path / docs_id / "manifest.yaml"
    if not manifest_path.exists():
        raise HTTPException(404, f"Documentation topic '{docs_id}' not found.")
    with manifest_path.open() as f:
        return yaml.safe_load(f) or {}


def _parse_manifest(docs_id: str, raw: dict[str, Any]) -> DocManifest:
    sections = [
        DocSection(
            id=s["id"],
            title=s["title"],
            file=s["file"],
            audience=s.get("audience", "all"),
            type=s.get("type", "markdown"),
        )
        for s in (raw.get("sections") or [])
    ]
    return DocManifest(
        docs_id=docs_id,
        title=raw.get("title", docs_id),
        description=raw.get("description"),
        pipelines=raw.get("pipelines") or [],
        thumbnail=raw.get("thumbnail"),
        sections=sections,
    )


def list_doc_topics(docs_path: Path) -> list[DocTopicSummary]:
    """Return a summary for every topic that has a valid manifest.yaml."""
    if not docs_path.is_dir():
        return []

    topics: list[DocTopicSummary] = []
    for entry in sorted(docs_path.iterdir()):
        if not entry.is_dir():
            continue
        manifest_file = entry / "manifest.yaml"
        if not manifest_file.exists():
            continue
        try:
            with manifest_file.open() as f:
                raw = yaml.safe_load(f) or {}
            topics.append(DocTopicSummary(
                docs_id=entry.name,
                title=raw.get("title", entry.name),
                description=raw.get("description"),
                pipelines=raw.get("pipelines") or [],
                thumbnail=raw.get("thumbnail"),
            ))
        except Exception:
            continue

    return topics


def get_doc_manifest(docs_path: Path, docs_id: str) -> DocManifest:
    _validate_docs_id(docs_id)
    raw = _load_manifest_raw(docs_path, docs_id)
    return _parse_manifest(docs_id, raw)


def resolve_doc_file(docs_path: Path, docs_id: str, path: str) -> Path:
    """
    Resolve and return the absolute path to a file within a docs topic folder.

    Raises HTTPException 400 on path traversal, 404 if not found.
    """
    _validate_docs_id(docs_id)

    topic_root = docs_path / docs_id
    if not topic_root.is_dir():
        raise HTTPException(404, f"Documentation topic '{docs_id}' not found.")

    try:
        resolved = (topic_root / path).resolve()
        assert_safe_path(topic_root, resolved)
    except PathEscapeError:
        raise HTTPException(400, "Invalid documentation file path.")
    except Exception:
        raise HTTPException(400, "Invalid documentation file path.")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, f"Documentation file '{path}' not found in topic '{docs_id}'.")

    return resolved
