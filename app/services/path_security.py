"""
Centralised path-containment and archive-security logic.

Every path derived from user input must pass through ``assert_safe_path`` before
any filesystem operation. Every zip extracted for a user must be extracted via
``safe_unzip``. No other code should perform raw path manipulation without
going through these primitives.
"""

import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath


class PathEscapeError(ValueError):
    """Raised when a path would escape its permitted base directory."""


class ArchiveTooLargeError(ValueError):
    """Raised when a zip's contents would exceed the permitted extracted size.

    Distinct from :class:`PathEscapeError` because it is a capacity failure, not
    a containment failure: callers map it to HTTP 413, alongside the upload size
    limits, rather than to 400.
    """


def assert_safe_path(base: Path, target: Path) -> None:
    """
    Verify that *target* is contained within *base*.

    Symlinks in *target* are resolved so that a link pointing outside *base*
    is caught. Raises ``PathEscapeError`` on any violation.
    """
    base_resolved = base.resolve()

    if not target.is_absolute():
        # Treat relative targets as relative to base
        candidate = (base_resolved / target).resolve()
    else:
        candidate = target.resolve()

    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise PathEscapeError(
            f"Path escapes allowed base: {candidate!r} is not under {base_resolved!r}"
        )


def assert_safe_upload_filename(filename: str, *, allow_subdirs: bool = False) -> None:
    """
    Validate a filename supplied via multipart upload.

    Rejects:
    - Empty or missing filenames.
    - Absolute paths (``/foo`` or Windows ``C:\\foo``).
    - Path traversal via ``..`` components.
    - Directory separators when *allow_subdirs* is False (flat uploads only).

    Does not touch the filesystem — purely lexical.
    """
    if not filename:
        raise PathEscapeError("Empty filename in upload")

    # Normalise backslashes so Windows paths are caught uniformly
    normalised = filename.replace("\\", "/")

    if os.path.isabs(normalised):
        raise PathEscapeError(f"Absolute filename not permitted: {filename!r}")

    if not allow_subdirs and "/" in normalised:
        raise PathEscapeError(
            f"Directory separators not permitted in filename: {filename!r}"
        )

    for part in PurePosixPath(normalised).parts:
        if part == "..":
            raise PathEscapeError(f"Path traversal in filename: {filename!r}")


# A subject identifier becomes a filename stem inside the project
# (``<modality>/<MRID>.nii.gz``) and is also substituted into tool command lines
# (e.g. dcm2niix ``-f {mrid}``), so it is restricted to characters that are safe
# in both roles: alphanumerics plus ``_``, ``-`` and ``.``, with a leading
# alphanumeric. That excludes path separators, ``.``/``..``, whitespace, control
# characters, and anything starting with ``-`` that a tool would read as a flag.
_MRID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def assert_safe_mrid(mrid: str) -> None:
    """
    Validate a subject identifier used to build a path or a tool argument.

    Raises ``PathEscapeError`` for an empty MRID, one containing a path
    separator or whitespace, one that is ``.`` or ``..``, one starting with
    ``.`` or ``-``, or one longer than 128 characters.

    Containment of the resulting path must still be checked with
    ``assert_safe_path``; this is a lexical check on the identifier itself.
    Does not touch the filesystem.
    """
    if not mrid:
        raise PathEscapeError("Empty subject ID")

    if not _MRID_RE.match(mrid):
        raise PathEscapeError(
            f"Invalid subject ID {mrid!r}: must start with a letter or digit and "
            "contain only letters, digits, '_', '-' and '.'"
        )


_MACOS_METADATA_DIR = "__MACOSX"


def is_skipped_member(name: str) -> bool:
    """True for upload members that are dropped rather than rejected.

    Any path component beginning with ``.`` (``.DS_Store``, ``.git``,
    ``._resource`` forks) and anything under ``__MACOSX/``. Finder-made zips
    almost always contain these, so failing the whole upload over them would
    break ordinary macOS users every time; they are never written, and the
    caller reports them separately from genuine rejections.

    Purely lexical, and deliberately *not* a security check: traversal,
    absolute paths and symlinks are rejected outright elsewhere.
    """
    if not name:
        return False
    normalised = name.replace("\\", "/")
    for part in PurePosixPath(normalised).parts:
        # "." and ".." are path navigation, not dotfiles. Treating them as
        # skippable would silently drop a traversal attempt instead of failing
        # the request, so they are left for the security checks to reject.
        if part in ("/", "", ".", ".."):
            continue
        if part == _MACOS_METADATA_DIR or part.startswith("."):
            return True
    return False


def assert_no_symlinks(root: Path) -> None:
    """Verify no symlink exists anywhere under *root*.

    Run after writing a staged upload and before moving it into place. The
    lexical checks cannot see a link that only exists once bytes are on disk,
    and a link created inside the staging area could otherwise redirect the
    subsequent move. Directories are checked too, since a linked directory
    would carry its whole subtree outside *root*.
    """
    if root.is_symlink():
        raise PathEscapeError(f"Staged upload root is a symlink: {root!r}")
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise PathEscapeError(
                f"Symlink not permitted in upload: {entry.relative_to(root)!r}"
            )


_EXTRACT_CHUNK_BYTES = 1024 * 1024


def _check_member(member: zipfile.ZipInfo, dest_resolved: Path) -> Path:
    """Validate one zip member and return the resolved path it extracts to.

    Rejects absolute member paths, traversal out of *dest_resolved*, and symlink
    members. Purely a check plus a path computation — nothing is written.
    """
    name = member.filename

    if os.path.isabs(name):
        raise PathEscapeError(f"Zip entry has absolute path: {name!r}")

    target = (dest_resolved / name).resolve()
    try:
        target.relative_to(dest_resolved)
    except ValueError:
        raise PathEscapeError(f"Zip entry escapes destination: {name!r}")

    # Unix file mode is in the upper 16 bits of external_attr
    unix_mode = member.external_attr >> 16
    if unix_mode != 0 and stat.S_ISLNK(unix_mode):
        raise PathEscapeError(f"Zip entry is a symlink: {name!r}")

    return target


def _assert_declared_size_fits(
    members: list[zipfile.ZipInfo], max_total_bytes: int
) -> None:
    """Reject an archive whose own central directory declares too much content.

    ``ZipInfo.file_size`` is read from the central directory, so this costs
    nothing and runs before a single byte is written. It stops a decompression
    bomb: deflate reaches roughly 1000:1 on low-entropy input, so without a
    bound a 1 GiB archive sitting exactly at the per-file upload limit expands
    to about a terabyte.

    ``file_size`` is a field the uploader controls, so the obvious question is
    whether an archive can understate it and expand past this bound anyway. It
    cannot, as long as extraction goes through ``ZipFile.open``: that reader
    stops after ``file_size`` uncompressed bytes regardless of how much
    compressed data follows (and then fails the CRC). Understating the size
    therefore truncates the attacker's own payload rather than evading the
    bound, which is why this one up-front check is the whole bound and not just
    a fast path.
    """
    declared = sum(m.file_size for m in members)
    if declared > max_total_bytes:
        raise ArchiveTooLargeError(
            f"Archive contents are {declared // (1024 * 1024)} MiB uncompressed, "
            f"over the {max_total_bytes // (1024 * 1024)} MiB limit. Nothing was "
            "extracted."
        )


def _extract_bounded(
    zf: zipfile.ZipFile,
    members: list[tuple[zipfile.ZipInfo, Path]],
    max_total_bytes: int,
) -> list[str]:
    """Write *members* out, member by member, in bounded chunks.

    On any failure — the size guard below, a corrupt member, a full disk —
    everything this call created is removed and the error re-raised, so a
    rejected archive leaves nothing behind even when it extracts straight into
    a live directory. Files and directories that existed beforehand are left
    alone.

    The running-total guard is a backstop, not the bound:
    :func:`_assert_declared_size_fits` has already rejected an archive that
    declares too much, and ``ZipFile.open`` will not yield more than the
    declared size. It is kept because it is three lines and the bound would
    otherwise rest entirely on that stdlib behaviour.

    Returns the member names written. Directory members are created but not
    reported.
    """
    written_files: list[Path] = []
    created_dirs: list[Path] = []
    extracted: list[str] = []
    total = 0

    def _mkdirs(d: Path) -> None:
        # Record only the directories that did not already exist, so cleanup
        # never removes a directory that predates this extraction.
        missing = []
        probe = d
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        d.mkdir(parents=True, exist_ok=True)
        created_dirs.extend(reversed(missing))

    try:
        for member, target in members:
            if member.is_dir():
                _mkdirs(target)
                continue
            _mkdirs(target.parent)
            with zf.open(member) as src, target.open("wb") as out:
                written_files.append(target)
                while chunk := src.read(_EXTRACT_CHUNK_BYTES):
                    total += len(chunk)
                    if total > max_total_bytes:
                        raise ArchiveTooLargeError(
                            "Archive expands to more than the "
                            f"{max_total_bytes // (1024 * 1024)} MiB limit. "
                            "Nothing was extracted."
                        )
                    out.write(chunk)
            extracted.append(member.filename)
    except BaseException:
        for path in written_files:
            path.unlink(missing_ok=True)
        for d in reversed(created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        raise

    return extracted


def safe_unzip_filtered(
    archive: Path, dest: Path, *, max_total_bytes: int
) -> tuple[list[str], list[str]]:
    """Extract *archive* into *dest*, skipping macOS/dotfile members.

    Applies every check ``safe_unzip`` does — absolute paths, traversal, symlink
    members and the *max_total_bytes* expansion bound — but members matching
    :func:`is_skipped_member` are omitted from the extraction instead of causing
    a failure.

    Returns ``(extracted_names, skipped_names)``. Directory entries are not
    reported in either list.
    """
    dest_resolved = dest.resolve()
    skipped: list[str] = []
    members: list[tuple[zipfile.ZipInfo, Path]] = []

    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            target = _check_member(member, dest_resolved)
            if member.is_dir():
                continue
            if is_skipped_member(member.filename):
                skipped.append(member.filename)
                continue
            members.append((member, target))

        # Skipped members are never written, so they do not count against the
        # size bound either.
        _assert_declared_size_fits([m for m, _ in members], max_total_bytes)
        extracted = _extract_bounded(zf, members, max_total_bytes)

    return extracted, skipped


def safe_unzip(archive: Path, dest: Path, *, max_total_bytes: int) -> None:
    """
    Extract *archive* into *dest*, rejecting any entry that:

    - Has an absolute member path.
    - Would resolve outside *dest* via path traversal (``..`` components).
    - Is a Unix symlink (detected via the external_attr field).

    and rejecting the archive as a whole if its contents exceed
    *max_total_bytes* uncompressed, which callers set from the per-request
    upload limit: one request may not land more on disk than that limit allows,
    whether it arrives compressed or not.

    All member paths and the declared content size are checked before any
    extraction begins, so a malicious archive cannot partially extract before
    the violation is detected. An archive that understates its own sizes is
    caught mid-stream instead, and anything already written is removed.

    *max_total_bytes* is keyword-only and has no default: an unbounded
    extraction is never the right thing, so a caller has to supply the bound
    rather than inherit one by omission.
    """
    dest_resolved = dest.resolve()
    members: list[tuple[zipfile.ZipInfo, Path]] = []

    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            members.append((member, _check_member(member, dest_resolved)))

        _assert_declared_size_fits(
            [m for m, _ in members if not m.is_dir()], max_total_bytes
        )
        _extract_bounded(zf, members, max_total_bytes)
