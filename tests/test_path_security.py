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

from app.services.path_security import (
    ArchiveTooLargeError,
    PathEscapeError,
    assert_safe_path,
    safe_unzip,
)


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


# A bound generous enough that no containment test trips it; the bound
# itself is exercised by its own tests below.
_CAP = 64 * 1024 * 1024


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
    safe_unzip(archive, dest, max_total_bytes=_CAP)
    assert (dest / "file.txt").read_bytes() == b"hello"
    assert (dest / "subdir" / "nested.txt").read_bytes() == b"world"


def test_safe_unzip_rejects_absolute_path(tmp_path):
    archive = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "root:x:0:0")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError, match="absolute path"):
        safe_unzip(archive, dest, max_total_bytes=_CAP)


def test_safe_unzip_rejects_traversal(tmp_path):
    archive = tmp_path / "evil_trav.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../outside.txt", "bad")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError):
        safe_unzip(archive, dest, max_total_bytes=_CAP)


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
        safe_unzip(archive, dest, max_total_bytes=_CAP)


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
        safe_unzip(archive, dest, max_total_bytes=_CAP)
    # Nothing should have been extracted
    assert not list(dest.iterdir())


# ── Upload member classification ──────────────────────────────────────────────

def test_is_skipped_member_matches_dotfiles_and_macos_metadata():
    from app.services.path_security import is_skipped_member

    assert is_skipped_member(".DS_Store")
    assert is_skipped_member("__MACOSX/._data.nii.gz")
    assert is_skipped_member("sub/.hidden/file.csv")
    assert is_skipped_member("._resource")


def test_is_skipped_member_does_not_swallow_path_navigation():
    """'..' begins with a dot but is a traversal attempt, not a dotfile.

    Classifying it as skippable would drop it quietly instead of failing the
    request, which is how a traversal could pass unnoticed.
    """
    from app.services.path_security import is_skipped_member

    assert not is_skipped_member("../escaped.csv")
    assert not is_skipped_member("a/../../etc/passwd")
    assert not is_skipped_member("./a.csv")
    assert not is_skipped_member("normal.csv")


def test_assert_no_symlinks_detects_a_link_written_into_staging(tmp_path):
    from app.services.path_security import PathEscapeError, assert_no_symlinks

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "real.csv").write_text("MRID\n")
    assert_no_symlinks(staging)  # clean

    (staging / "link.csv").symlink_to("/etc/passwd")
    with pytest.raises(PathEscapeError):
        assert_no_symlinks(staging)


def test_assert_no_symlinks_detects_a_linked_directory(tmp_path):
    """A linked directory would carry its whole subtree outside the root."""
    from app.services.path_security import PathEscapeError, assert_no_symlinks

    outside = tmp_path / "outside"
    outside.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "sub").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapeError):
        assert_no_symlinks(staging)


# ── Filtering unzip ───────────────────────────────────────────────────────────

def _zip(tmp_path, entries, name="a.zip"):
    import zipfile

    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in entries.items():
            zf.writestr(member, data)
    return path


def test_safe_unzip_filtered_skips_dotfiles_and_extracts_the_rest(tmp_path):
    from app.services.path_security import safe_unzip_filtered

    archive = _zip(tmp_path, {
        "data.csv": "MRID\n",
        ".DS_Store": "junk",
        "__MACOSX/._data.csv": "junk",
    })
    dest = tmp_path / "out"
    dest.mkdir()
    extracted, skipped = safe_unzip_filtered(archive, dest, max_total_bytes=_CAP)

    assert extracted == ["data.csv"]
    assert sorted(skipped) == sorted([".DS_Store", "__MACOSX/._data.csv"])
    assert (dest / "data.csv").exists()
    assert not (dest / ".DS_Store").exists()
    assert not (dest / "__MACOSX").exists()


def test_safe_unzip_filtered_rejects_traversal_before_writing_anything(tmp_path):
    from app.services.path_security import PathEscapeError, safe_unzip_filtered

    archive = _zip(tmp_path, {"good.csv": "MRID\n", "../escaped.csv": "x"})
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError):
        safe_unzip_filtered(archive, dest, max_total_bytes=_CAP)
    assert list(dest.iterdir()) == []


def test_safe_unzip_filtered_rejects_symlink_members(tmp_path):
    import zipfile

    from app.services.path_security import PathEscapeError, safe_unzip_filtered

    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("good.csv", "MRID\n")

    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PathEscapeError):
        safe_unzip_filtered(archive, dest, max_total_bytes=_CAP)
    assert list(dest.iterdir()) == []


# ── Expansion bound (decompression bombs) ─────────────────────────────────────

def _bomb(archive: Path, uncompressed: int) -> None:
    """A zip whose single member expands to *uncompressed* bytes of zeros."""
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\0" * uncompressed)


def test_safe_unzip_rejects_a_declared_oversize_archive(tmp_path):
    """The cheap path: the central directory says it is too big, nothing is written."""
    archive = tmp_path / "bomb.zip"
    _bomb(archive, 4 * 1024 * 1024)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ArchiveTooLargeError, match="uncompressed"):
        safe_unzip(archive, dest, max_total_bytes=1024 * 1024)
    assert not list(dest.iterdir())


def test_safe_unzip_compression_ratio_is_what_makes_this_necessary(tmp_path):
    """Document the ratio the bound exists to stop: the archive is tiny."""
    archive = tmp_path / "bomb.zip"
    _bomb(archive, 8 * 1024 * 1024)
    assert archive.stat().st_size < 64 * 1024  # >100:1 on this payload


def _understated(archive: Path, uncompressed: int) -> Path:
    """A copy of *archive* whose declared file_size is rewritten to 1 byte.

    Patches both the local header and the central directory, leaving the
    compressed data untouched.
    """
    raw = archive.read_bytes()
    original = uncompressed.to_bytes(4, "little")
    assert raw.count(original) >= 2, "expected file_size in both headers"
    patched = archive.with_name("patched.zip")
    patched.write_bytes(raw.replace(original, (1).to_bytes(4, "little")))
    with zipfile.ZipFile(patched) as zf:
        # The patched member is whichever one declared *uncompressed*.
        assert any(m.file_size == 1 for m in zf.infolist())
        assert not any(m.file_size == uncompressed for m in zf.infolist())
    return patched


def test_understating_file_size_cannot_evade_the_bound(tmp_path):
    """Why the declared-size check is the whole bound, not just a fast path.

    ``file_size`` is attacker-controlled, so an archive can claim to be small.
    ``ZipFile.open`` stops after that many uncompressed bytes, so the lie
    truncates the attacker's own payload instead of evading the limit — here it
    fails the CRC, and in no case does it yield more than was declared.
    """
    archive = tmp_path / "bomb.zip"
    _bomb(archive, 4 * 1024 * 1024)
    patched = _understated(archive, 4 * 1024 * 1024)

    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(zipfile.BadZipFile):
        safe_unzip(patched, dest, max_total_bytes=1024 * 1024)

    with zipfile.ZipFile(patched) as zf, zf.open(zf.infolist()[-1]) as member:
        with pytest.raises(zipfile.BadZipFile):
            member.read()  # never returns more than the declared 1 byte


def test_safe_unzip_cleans_up_what_it_wrote_before_aborting(tmp_path):
    """A failure mid-extraction must not leave files in a live directory."""
    archive = tmp_path / "liar.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("small.txt", b"ok")
        zf.writestr("nested/also_small.txt", b"ok")
        zf.writestr("big.bin", b"\0" * (4 * 1024 * 1024))
    patched = _understated(archive.with_name("liar.zip"), 4 * 1024 * 1024)

    # A pre-existing file must survive; only what this call created is removed.
    dest = tmp_path / "live"
    dest.mkdir()
    (dest / "pre_existing.txt").write_text("keep me")

    with pytest.raises(zipfile.BadZipFile):
        safe_unzip(patched, dest, max_total_bytes=_CAP)

    assert [p.name for p in dest.iterdir()] == ["pre_existing.txt"]


def test_safe_unzip_accepts_an_archive_inside_the_bound(tmp_path):
    archive = tmp_path / "fine.zip"
    _bomb(archive, 512 * 1024)
    dest = tmp_path / "out"
    dest.mkdir()
    safe_unzip(archive, dest, max_total_bytes=1024 * 1024)
    assert (dest / "bomb.bin").stat().st_size == 512 * 1024


def test_safe_unzip_still_creates_directory_members(tmp_path):
    """Streaming extraction must not drop empty directories the archive declares."""
    archive = tmp_path / "dirs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("empty_dir/", b"")
        zf.writestr("has/a/file.txt", b"x")
    dest = tmp_path / "out"
    dest.mkdir()
    safe_unzip(archive, dest, max_total_bytes=_CAP)
    assert (dest / "empty_dir").is_dir()
    assert (dest / "has" / "a" / "file.txt").read_bytes() == b"x"


def test_safe_unzip_filtered_rejects_an_oversize_archive(tmp_path):
    from app.services.path_security import safe_unzip_filtered

    archive = tmp_path / "bomb.zip"
    _bomb(archive, 4 * 1024 * 1024)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ArchiveTooLargeError):
        safe_unzip_filtered(archive, dest, max_total_bytes=1024 * 1024)
    assert not list(dest.iterdir())


def test_safe_unzip_filtered_does_not_count_skipped_members(tmp_path):
    """A dotfile is never written, so it must not push the archive over the bound."""
    from app.services.path_security import safe_unzip_filtered

    archive = tmp_path / "mac.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("__MACOSX/._big", b"\0" * (4 * 1024 * 1024))
        zf.writestr("real.txt", b"ok")
    dest = tmp_path / "out"
    dest.mkdir()
    extracted, skipped = safe_unzip_filtered(archive, dest, max_total_bytes=1024 * 1024)
    assert extracted == ["real.txt"]
    assert skipped == ["__MACOSX/._big"]
