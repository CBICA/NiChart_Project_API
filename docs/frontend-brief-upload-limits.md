# Frontend brief — upload size limits and subject-ID validation

Two API changes that the upload screens need to account for. Both are live on the
API `master` working tree; `openapi.json` at the repo root is regenerated and
includes them.

1. Every upload endpoint now enforces a size limit and answers **413** when it is
   exceeded. A new public `GET /limits` reports the limits so you can check a file
   before transferring it.
2. The NIfTI commit and DICOM convert endpoints now validate the subject ID
   (MRID) and answer **400** for IDs they used to accept. The review table has to
   validate and trim before sending.

---

## 1. Upload size limits

### Reading the limits

```
GET /limits            → { "max_upload_file_mb": 1024, "max_upload_request_mb": 10240 }
```

Public — no auth, in either mode. The values are deployment configuration, so
**fetch them at startup rather than hard-coding 1024/10240.** A self-hosted or
on-prem deployment may run much lower limits, and a UI with the numbers baked in
will tell the user the wrong thing.

- `max_upload_file_mb` — the largest single file any endpoint accepts.
- `max_upload_request_mb` — the largest total across all files in one request,
  and also the largest uncompressed total a `.zip` may expand to (see below).

One pair of limits covers every upload endpoint; there is no per-endpoint
variation to special-case.

### The error

Either limit produces `413` with the standard error shape:

```json
{ "detail": "'brain_scan.nii.gz' is larger than the 1024 MiB per-file limit. Nothing was saved." }
```

```json
{ "detail": "This upload exceeds the 10240 MiB per-request limit. Nothing was saved; send fewer files per request." }
```

`detail` is written to be shown to the user as-is and always names the limit. The
two cases are distinguishable by the text (`per-file` vs `per-request`) if you
need different handling, but treating them identically is reasonable.

**Nothing is saved on a 413 — the whole request fails, including the files that
were under the limit.** One oversized file in a batch of two hundred fails all two
hundred; no staging area is created and no partial state is left behind. This is
deliberate: at a limit of a gigabyte an oversized file is a mistake, not one odd
file in an otherwise good batch, and a single error shape is easier to handle than
a partial-success response. If per-file rejection within a batch turns out to be
what users need, say so and we will add a `rejected: [{ name, reason }]` list
instead — the intermediate-upload endpoints already answer that way, so there is a
precedent to copy.

### What this means for the UI — the important part

**Check sizes in the browser before uploading.** The server cannot reject early:
the whole request body is received before the route function runs, so a 413 only
arrives *after* the user has waited out the entire transfer. For a multi-gigabyte
upload on a slow connection that is a long wait ending in failure.

So the browser check is the real user-facing limit, and the 413 is a backstop for
clients that skip it:

```js
const { max_upload_file_mb, max_upload_request_mb } = await fetch('/limits').then(r => r.json());

const tooBig = files.filter(f => f.size > max_upload_file_mb * 1024 * 1024);
const total  = files.reduce((n, f) => n + f.size, 0);

if (tooBig.length) { /* name them, refuse, do not send */ }
if (total > max_upload_request_mb * 1024 * 1024) { /* offer to split into batches */ }
```

Worth surfacing the limits in the upload UI before a file is chosen ("up to 1 GiB
per file, 10 GiB per upload"), so the constraint is visible rather than discovered.

### Batching

When a selection exceeds the per-request limit but every file is individually
fine, splitting into several requests works. Two notes:

- **NIfTI**: each request returns its own `staging_id`, and commit is per staging
  area. So N batches means N upload → review → commit cycles, or holding several
  staging IDs and committing each. Consider batching by total size rather than
  file count.
- **DICOM / BIDS / IDAT**: each batch stages independently; for DICOM each
  request yields its own `staging_id` to inspect and convert separately.

### Affected endpoints

All of these can now answer 413:

```
POST /projects/{id}/files/upload/nifti
POST /projects/{id}/files/upload/nifti/zip
POST /projects/{id}/files/upload/csv
POST /projects/{id}/files/upload/bids
POST /projects/{id}/files/upload/bids/files
POST /projects/{id}/files/upload/idat
POST /projects/{id}/files/upload/idat/files
POST /projects/{id}/files/dicom/upload
POST /projects/{id}/files/dicom/upload/files
```

(The two `upload/intermediate` endpoints already enforced the same limits and are
unchanged.)

### Zip uploads: a second 413 you cannot pre-check

A `.zip` is also limited by what it contains, not only by its own size: the
uncompressed total may not exceed `max_upload_request_mb`. The same limit, so
there is nothing extra to fetch — but it is the one 413 a browser-side size
check cannot predict, because the uncompressed size is not something the UI
knows from the `File` object.

```json
{ "detail": "Archive contents are 24576 MiB uncompressed, over the 10240 MiB limit. Nothing was extracted." }
```

Nothing is extracted and nothing is saved. Applies to the four zip endpoints
(`nifti/zip`, `bids`, `idat`, `dicom/upload`) and to `upload/intermediate/zip`.

Practically: keep the pre-check on the file's own size as described above, and
treat this one as a server-side error to display. If it turns out users hit it
with legitimate data — a large uncompressed-DICOM study is the plausible case —
the fix is a higher deployment limit, not a client change. Worth including the
`detail` text verbatim in any bug report, since it names both numbers.

### One deployment caveat

In a deployed environment a proxy in front of the API may also enforce a body-size
limit. That rejection comes from the proxy, not the API, so it may be an HTML
error page rather than `{"detail": ...}`, or the connection may simply reset
mid-upload. **Do not assume a 413 response body is JSON** — parse defensively and
fall back to a generic "file too large" message. The browser-side pre-check is
what keeps users out of this path.

---

## 2. Subject ID (MRID) validation

### The rule

An MRID must match:

```
^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$
```

A letter or digit first, then letters, digits, `_`, `-` and `.`, up to 128
characters. No spaces, no `/` or `\`, no leading `.` or `-`, not `.` or `..`, not
empty.

The MRID becomes a filename on disk and is also passed to the conversion tool as a
command-line argument, which is why the character set is narrow. Normal subject
IDs are unaffected — `IXI002-Guys-0828`, `sub001`, `01_ses-1` and `sub.01` all
pass.

### Where it applies

```
POST /projects/{id}/files/stage/{staging_id}/commit   — every mappings[].mrid
POST /projects/{id}/files/dicom/{staging_id}/convert  — every series_mappings[].mrid
```

Both answer `400` with `{"detail": "..."}` naming the offending value.

### What the UI needs to change

The review table currently sends what the user typed, unmodified. Two concrete
consequences:

- **Trim whitespace.** A leading or trailing space is now rejected. This is the
  most likely new failure in practice — a trailing space is invisible in the input
  and used to be accepted.
- **Validate before submitting.** Apply the pattern above as you would any form
  validation, per row, and block the commit while any row is invalid. Doing this
  client-side is much better UX than a 400 after the fact, because the commit is
  all-or-nothing: one bad MRID rejects the whole batch and nothing moves out of
  staging (the staging area is left intact, so the user can fix the row and retry
  the same `staging_id`).

Server-inferred values (`inferred_mrid`, `basename_mrid`) always satisfy the rule,
so a user who accepts the proposals as-is never sees a 400. Only typed input can
fail.

For DICOM convert, `mrid` stays optional: omit it, or send `""`, and the server
falls back to the DICOM `PatientID`, then to `"subject"`. If the `PatientID` in
the file does not satisfy the rule, the response is a 400 naming that series and
asking for an explicit `mrid` — so a "set the subject ID for this series" input is
worth having in the series-mapping UI even though the field is optional.

---

## What has not changed

- No request or response shapes changed. No new required fields, no removed
  fields. `413` and the stricter `400` are additions.
- Upload semantics are otherwise identical: same endpoints, same staging flows,
  same success responses.
- A UI that does none of the above still works for ordinary files and ordinary
  subject IDs. The changes matter for large files and for hand-typed MRIDs.
