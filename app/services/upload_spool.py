"""
Size-capped streaming of multipart uploads to disk.

Every ordinary upload route writes its parts through :class:`UploadBudget` rather
than calling ``await upload.read()``, which would materialise a whole file in
memory at once. The caps come from ``NICHART_MAX_UPLOAD_FILE_MB`` and
``NICHART_MAX_UPLOAD_REQUEST_MB`` — the same two settings the intermediate-upload
path already uses, so there is one limit across the API rather than one per
endpoint.

Files are spooled into ``{project}/_upload/_spool/{uuid}/``, not into the system
temp directory, for two reasons: it is on the same filesystem as the project, so
handing the file to a service is a rename rather than a multi-gigabyte copy; and
``_upload`` is already excluded from project listings and from the S3 sync.

What this does and does not bound: it bounds the memory the server uses and the
bytes it keeps. It does not bound what the client can transfer, because Starlette
parses and spools the entire multipart body to disk before a route function is
entered. A limit that rejects before the body arrives has to be enforced on
``Content-Length`` by whatever fronts the API.
"""

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import Settings
from app.services.path_security import assert_safe_path

_CHUNK_BYTES = 1024 * 1024

_SPOOL_SUBDIR = Path("_upload") / "_spool"


def extracted_cap(settings: Settings) -> int:
    """The bound on the uncompressed contents of an uploaded archive, in bytes.

    Deliberately the same number as the per-request cap: one request may not
    land more on disk than that limit allows, whether it arrives compressed or
    not. Reusing it keeps one limit across the API instead of adding a second
    one a client would have to discover separately — and ``GET /limits`` already
    reports it.

    Without such a bound the per-file cap is misleading rather than protective:
    deflate reaches roughly 1000:1 on low-entropy input, so a 1 GiB archive
    sitting exactly at the per-file limit expands to about a terabyte.
    """
    return settings.max_upload_request_mb * 1024 * 1024


@contextmanager
def spool_dir(project_path: Path) -> Iterator[Path]:
    """
    A private directory for one request's spooled uploads, removed on the way out.

    Removed whether the request succeeds or fails, so a rejected upload leaves
    nothing behind. Anything a service has already moved out is unaffected.
    """
    d = project_path / _SPOOL_SUBDIR / uuid.uuid4().hex
    assert_safe_path(project_path, d)
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class UploadBudget:
    """
    Streams uploads to disk, enforcing a per-file and a per-request cap.

    One instance per request: ``total`` accumulates across every file spooled
    through it, which is what the per-request cap is measured against.

    Both caps raise ``HTTPException(413)``. A single file over the per-file cap
    fails the whole request rather than being dropped from a batch: at a limit of
    a gigabyte, an oversized part is a mistake or an attack, not one odd file in
    an otherwise good batch, and one error shape is easier for a client to handle
    than two.
    """

    def __init__(self, settings: Settings) -> None:
        self.file_cap = settings.max_upload_file_mb * 1024 * 1024
        self.request_cap = settings.max_upload_request_mb * 1024 * 1024
        self._file_cap_mb = settings.max_upload_file_mb
        self._request_cap_mb = settings.max_upload_request_mb
        self.total = 0

    async def spool(self, upload: UploadFile, dest: Path) -> int:
        """
        Write *upload* to *dest* in chunks. Returns the number of bytes written.

        Raises ``HTTPException(413)`` if this file exceeds the per-file cap or if
        the request's running total exceeds the per-request cap. The partial file
        is removed before raising, so *dest* never holds a truncated upload.
        """
        name = upload.filename or "(unnamed)"
        size = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open("wb") as out:
                while chunk := await upload.read(_CHUNK_BYTES):
                    size += len(chunk)
                    if size > self.file_cap:
                        raise HTTPException(
                            413,
                            f"'{name}' is larger than the {self._file_cap_mb} MiB "
                            "per-file limit. Nothing was saved.",
                        )
                    if self.total + size > self.request_cap:
                        raise HTTPException(
                            413,
                            f"This upload exceeds the {self._request_cap_mb} MiB "
                            "per-request limit. Nothing was saved; send fewer "
                            "files per request.",
                        )
                    out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

        self.total += size
        return size
