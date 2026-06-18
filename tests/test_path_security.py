"""
Unit tests for app.services.path_security.

Tests cover all rejection conditions documented in CLAUDE.md:
- Path containment (absolute, relative, symlink traversal)
- Zip extraction: absolute member paths, traversal, symlinks
"""

import stat
import zipfile
from pathlib import Path

import pytest

from app.services.path_security import PathEscapeError, assert_safe_path, safe_unzip


# ── assert_safe_path ──────────────────────────────────────────────────────────

def test_safe_path_exact_base(tmp_path):
    assert_safe_path(tmp_path, tmp_path)  # base itself is allowed


def test_safe_path_child(tmp_path):
    child = tmp_path / "subdir" / "file.txt"
    assert_safe_path(tmp_path, child)


def test_safe_path_relative_child(tmp_path):
    assert_safe_path(tmp_path, Path("subdir/file.txt"))


def test_safe_path_rejects_parent(tmp_path):
    with pytest.raises(PathEscapeError):
        assert_safe_path(tmp_path, tmp_path.parent)


def test_safe_path_rejects_dotdot_relative(tmp_path):
    with pytest.raises(PathEscapeError):
        assert_safe_path(tmp_path / "a", Path("../../etc/passwd"))


def test_safe_path_rejects_absolute_escape(tmp_path):
    with pytest.raises(PathEscapeError):
        assert_safe_path(tmp_path, Path("/etc/passwd"))


def test_safe_path_rejects_symlink_escape(tmp_path):
    """A symlink within the base that points outside must be rejected."""
    link = tmp_path / "escape"
    link.symlink_to(tmp_path.parent)  # points outside base
    with pytest.raises(PathEscapeError):
        assert_safe_path(tmp_path, link / "secret.txt")


# ── safe_unzip ────────────────────────────────────────────────────────────────

def _make_zip(archive: Path, members: dict[str, bytes | None], symlinks: list[str] | None = None) -> None:
    """
    Write a zip to *archive*.

    ``members`` maps member name → bytes content (None for directories).
    ``symlinks`` is a list of member names to mark as symlinks in external_attr.
    """
    symlinks = symlinks or []
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in members.items():
            if name in symlinks:
                zi = zipfile.ZipInfo(name)
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(zi, content or b"")
            elif content is None:
                zf.mkdir(name)
            else:
                zf.writestr(name, content)


def test_safe_unzip_normal(tmp_path):
    archive = tmp_path / "good.zip"
    _make_zip(archive, {"file.txt": b"hello", "subdir/nested.txt": b"world"})
    dest = tmp_path / "out"
    dest.mkdir()
    safe_unzip(archive, dest)
    assert (dest / "file.txt").read_bytes() == b"hello"
    assert (dest / "subdir" / "nested.txt").read_bytes() == b"world"


def test_safe_unzip_rejects_absolute_path(tmp_path):
    archive = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "root:x:0:0")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError, match="absolute path"):
        safe_unzip(archive, dest)


def test_safe_unzip_rejects_traversal(tmp_path):
    archive = tmp_path / "evil_trav.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../outside.txt", "bad")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError):
        safe_unzip(archive, dest)


def test_safe_unzip_rejects_symlink(tmp_path):
    archive = tmp_path / "evil_link.zip"
    _make_zip(
        archive,
        {"link_to_etc": b"/etc"},
        symlinks=["link_to_etc"],
    )
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError, match="symlink"):
        safe_unzip(archive, dest)


def test_safe_unzip_no_partial_extraction(tmp_path):
    """Validation runs before extraction — a bad entry in the middle must prevent all extraction."""
    archive = tmp_path / "mixed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("safe.txt", "ok")
        zf.writestr("../../evil.txt", "bad")
        zf.writestr("also_safe.txt", "ok")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError):
        safe_unzip(archive, dest)
    # Nothing should have been extracted
    assert not list(dest.iterdir())
