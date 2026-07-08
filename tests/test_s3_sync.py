"""
Tests for the S3 sync helpers.

The download path must be resilient to FSx DRA "directory marker" objects —
zero-byte S3 objects at a directory's key (no trailing slash) that collide with
the real directory of the same name on the local filesystem. Downloading one
onto a local directory raises IsADirectoryError, which previously crashed the
pipeline background task.
"""

from pathlib import Path

from app.services import s3_sync_service


class _Paginator:
    def __init__(self, contents):
        self._contents = contents

    def paginate(self, Bucket, Prefix):
        yield {"Contents": self._contents}


class _FakeS3:
    def __init__(self, contents):
        self._contents = contents
        self.downloaded: list[str] = []

    def get_paginator(self, name):
        return _Paginator(self._contents)

    def download_file(self, bucket, key, path):
        self.downloaded.append(key)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("x")


def test_download_sync_skips_directory_markers(tmp_path, monkeypatch):
    prefix = "fsx/U/proj"
    markers = {f"{prefix}/dlmuse_seg", f"{prefix}/dlmuse_vol"}
    keys = [
        f"{prefix}/t1/sub1.nii.gz",
        f"{prefix}/dlmuse_seg",                    # directory marker
        f"{prefix}/dlmuse_seg/sub1_seg.nii.gz",
        f"{prefix}/dlmuse_vol",                    # directory marker
        f"{prefix}/dlmuse_vol/vol.csv",
    ]
    contents = [{"Key": k, "Size": 0 if k in markers else 10} for k in keys]
    fake = _FakeS3(contents)
    monkeypatch.setattr(s3_sync_service.boto3, "client", lambda svc: fake)

    n = s3_sync_service._download_sync("bucket", prefix, tmp_path)

    # Markers never downloaded (no IsADirectoryError), real files all pulled.
    assert markers.isdisjoint(fake.downloaded)
    assert f"{prefix}/dlmuse_seg/sub1_seg.nii.gz" in fake.downloaded
    assert f"{prefix}/dlmuse_vol/vol.csv" in fake.downloaded
    assert f"{prefix}/t1/sub1.nii.gz" in fake.downloaded
    assert n == 3


def test_download_sync_skips_object_when_path_is_existing_dir(tmp_path, monkeypatch):
    """Belt-and-suspenders: a stray marker whose local path is already a dir is skipped."""
    prefix = "fsx/U/proj"
    # Only the marker is listed (no sibling file to trigger the dir_keys heuristic),
    # but the local path already exists as a directory.
    (tmp_path / "dlmuse_seg").mkdir()
    contents = [{"Key": f"{prefix}/dlmuse_seg", "Size": 0}]
    fake = _FakeS3(contents)
    monkeypatch.setattr(s3_sync_service.boto3, "client", lambda svc: fake)

    n = s3_sync_service._download_sync("bucket", prefix, tmp_path)

    assert fake.downloaded == []
    assert n == 0
