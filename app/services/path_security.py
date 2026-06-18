"""
Centralised path-containment and archive-security logic.

Every path derived from user input must pass through ``assert_safe_path`` before
any filesystem operation. Every zip extracted for a user must be extracted via
``safe_unzip``. No other code should perform raw path manipulation without
going through these primitives.
"""

import os
import stat
import zipfile
from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a path would escape its permitted base directory."""


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


def safe_unzip(archive: Path, dest: Path) -> None:
    """
    Extract *archive* into *dest*, rejecting any entry that:

    - Has an absolute member path.
    - Would resolve outside *dest* via path traversal (``..`` components).
    - Is a Unix symlink (detected via the external_attr field).

    All member paths are checked before any extraction begins, so a malicious
    archive cannot partially extract before the violation is detected.
    """
    dest_resolved = dest.resolve()

    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            name = member.filename

            # Reject absolute paths
            if os.path.isabs(name):
                raise PathEscapeError(
                    f"Zip entry has absolute path: {name!r}"
                )

            # Reject path traversal
            target = (dest_resolved / name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                raise PathEscapeError(
                    f"Zip entry escapes destination: {name!r}"
                )

            # Reject symlinks — Unix file mode is in the upper 16 bits of external_attr
            unix_mode = member.external_attr >> 16
            if unix_mode != 0 and stat.S_ISLNK(unix_mode):
                raise PathEscapeError(
                    f"Zip entry is a symlink: {name!r}"
                )

        # All entries are safe; extract
        zf.extractall(dest)
