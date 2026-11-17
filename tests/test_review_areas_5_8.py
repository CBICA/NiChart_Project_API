"""
Tests for the API side of the review fixes in area 7 (2026-11-17):

- A project's "Created" date is recorded once and doesn't move when folders are added (7.7).
- Zipping the whole project leaves out the app's hidden folders (7.7).
- Archive links: a short-lived URL that streams a selection as a zip, so the browser saves
  it to disk instead of holding it in page memory (7.4).
"""

import io
import json
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone


def _project(client, name):
    assert client.post("/projects", json={"name": name}).status_code == 201
    return name


def _created(client, pid):
    return next(p["created_at"] for p in client.get("/projects").json() if p["id"] == pid)


# ── Creation date ─────────────────────────────────────────────────────────────

def test_created_date_is_stable(data_client, tmp_path):
    pid = _project(data_client, "dates")
    first = _created(data_client, pid)
    time.sleep(0.02)
    (tmp_path / "LOCAL_USER" / pid / "t1").mkdir()  # changes the folder's mtime
    assert _created(data_client, pid) == first


def test_created_date_for_older_project_is_oldest_file(data_client, tmp_path):
    pdir = tmp_path / "LOCAL_USER" / "legacy"
    (pdir / "t1").mkdir(parents=True)
    old = (pdir / "t1" / "a.nii.gz")
    old.write_bytes(b"x")
    then = time.time() - 30 * 86400
    os.utime(old, (then, then))
    created = datetime.fromisoformat(_created(data_client, "legacy"))
    assert abs(created.timestamp() - then) < 5
    # Recorded, so it stays even once that file is gone.
    old.unlink()
    assert abs(datetime.fromisoformat(_created(data_client, "legacy")).timestamp() - then) < 5


# ── Whole-project zip ─────────────────────────────────────────────────────────

def test_project_zip_leaves_out_hidden_folders(data_client, tmp_path):
    pid = _project(data_client, "zipme")
    pdir = tmp_path / "LOCAL_USER" / pid
    (pdir / "t1").mkdir()
    (pdir / "t1" / "a.nii.gz").write_bytes(b"x")
    (pdir / "_upload" / "dicoms").mkdir(parents=True)
    (pdir / "_upload" / "dicoms" / "raw.dcm").write_bytes(b"secret")
    resp = data_client.get(f"/projects/{pid}/files/content", params={"path": ".", "zip": "true"})
    assert resp.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "t1/a.nii.gz" in names
    assert not any(n.startswith(("_upload", "_working")) for n in names)


# ── Archive links ─────────────────────────────────────────────────────────────

def _files(tmp_path, pid):
    pdir = tmp_path / "LOCAL_USER" / pid
    (pdir / "out" / "s1").mkdir(parents=True)
    (pdir / "out" / "s1" / "seg.nii.gz").write_bytes(b"abc")
    (pdir / "out" / "batch.csv").write_text("MRID\ns1\n")
    return pdir


def test_archive_link_streams_selection(data_client, tmp_path):
    pid = _project(data_client, "links")
    _files(tmp_path, pid)
    resp = data_client.post(f"/projects/{pid}/files/archive/link",
                            json={"paths": ["out/s1", "out/batch.csv"], "filename": "my-results"})
    assert resp.status_code == 200, resp.text
    link = resp.json()
    assert link["file_count"] == 2
    assert link["total_bytes"] == 3 + len("MRID\ns1\n")
    zresp = data_client.get(link["url"])
    assert zresp.status_code == 200
    assert 'filename="my-results.zip"' in zresp.headers["content-disposition"]
    names = sorted(zipfile.ZipFile(io.BytesIO(zresp.content)).namelist())
    assert names == ["out/batch.csv", "out/s1/seg.nii.gz"]
    # Reusable until it expires (the browser may retry).
    assert data_client.get(link["url"]).status_code == 200


def test_archive_link_rejects_bad_paths_up_front(data_client, tmp_path):
    pid = _project(data_client, "badlinks")
    assert data_client.post(f"/projects/{pid}/files/archive/link",
                            json={"paths": ["nope.csv"]}).status_code == 404
    assert data_client.post(f"/projects/{pid}/files/archive/link",
                            json={"paths": ["../../etc"]}).status_code == 400
    assert data_client.post(f"/projects/{pid}/files/archive/link",
                            json={"paths": ["x"], "filename": "../evil"}).status_code == 422


def test_archive_link_is_bound_to_project_and_expires(data_client, tmp_path):
    pid = _project(data_client, "bound")
    other = _project(data_client, "other")
    _files(tmp_path, pid)
    link = data_client.post(f"/projects/{pid}/files/archive/link", json={"paths": ["out"]}).json()
    token = link["url"].rsplit("/", 1)[1]
    assert data_client.get(f"/projects/{other}/files/archive/{token}").status_code == 404
    assert data_client.get(f"/projects/{pid}/files/archive/not-a-real-token-at-all").status_code == 404

    record = tmp_path / "_working" / "archive_links" / f"{token}.json"
    data = json.loads(record.read_text())
    data["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    record.write_text(json.dumps(data))
    assert data_client.get(link["url"]).status_code == 404
