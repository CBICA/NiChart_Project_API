"""
Subject coverage of a pipeline step: which subjects it was given, which it
produced output for, and why the others are missing.

A tool may drop subjects without failing — DLMUSE, say, writing no
segmentation for a scan that fails its QC. Nothing here asks the tool to
report that. After the step runs, the orchestrator compares

- the subjects in the step's inputs (``expected``) with
- the subjects its outputs cover (``produced``), as declared by the tool YAML's
  ``outputs.<slot>.contents`` (``{MRID}`` file patterns and
  ``rows_per_subject`` CSVs),

so a dropped subject is detected the same way for every tool and every
execution backend. A tool may also write a manifest about its run
(``outputs.<slot>.manifest``). Its format is the tool's own, so it is recorded
and shown to the user as written (an excerpt is kept with the coverage); only
when the YAML declares its ``mrid_column`` is it also read to attach a reason
to each dropped subject.

A subject in the run's scope that never reached the step is traced to where it
was lost: the upstream step that dropped it (with that step's reason), an
upstream step that never had it, or a project input that lacks it (no image,
no participants.csv row). That is what lets a downstream step's missing
subjects be told apart from its own drops.

Everything here reads directory listings and streams one CSV column, so cost
does not grow with image size. Nothing raises on unreadable data: an unknown
set is ``None`` ("can't tell"), never empty ("all missing").
"""

from __future__ import annotations

import csv
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app import modalities
from app.backends.base import ContentRule, ManifestSpec

_NIFTI_SUFFIXES = (".nii.gz", ".nii")
_MODALITY_DIRS = frozenset(m.dir_name for m in modalities.MODALITIES)
# A pattern is matched against at most this many directory entries; a slot
# holding more is reported as uncheckable rather than walked forever.
_MAX_ENTRIES = 500_000


# ── Reading subjects off disk ─────────────────────────────────────────────────

def _glob_regex(part: str) -> str:
    out = []
    for ch in part:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """Regex for a contents pattern: ``{MRID}`` captures the subject, the rest is a glob."""
    parts = pattern.split("{MRID}")
    body = _glob_regex(parts[0])
    for i, part in enumerate(parts[1:]):
        body += ("(?P<mrid>[^/]+?)" if i == 0 else "(?P=mrid)") + _glob_regex(part)
    return re.compile(body)


def _entries(directory: Path, recursive: bool) -> list[str] | None:
    """File paths under *directory*, relative to it. None if it can't be listed."""
    found: list[str] = []
    stack = [directory]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        if recursive:
                            stack.append(Path(e.path))
                    else:
                        found.append(os.path.relpath(e.path, directory).replace(os.sep, "/"))
                    if len(found) > _MAX_ENTRIES:
                        return None
    except OSError:
        return None
    return found


def subjects_matching(directory: Path, pattern: str) -> set[str] | None:
    """Subjects with a file matching *pattern* in *directory*."""
    names = _entries(directory, recursive="/" in pattern)
    if names is None:
        return None
    rx = pattern_regex(pattern)
    out: set[str] = set()
    for name in names:
        m = rx.fullmatch(name)
        if m and m.group("mrid"):
            out.add(m.group("mrid"))
    return out


def _column_index(header: list[str], column: str) -> int | None:
    want = column.strip().lower()
    for i, h in enumerate(header):
        if h.strip().lower() == want:
            return i
    return None


def read_csv_column(path: Path, column: str = "MRID") -> set[str] | None:
    """Non-blank values of one CSV column, streamed. None if absent or unreadable."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return None
            idx = _column_index(header, column)
            if idx is None:
                return None
            out: set[str] = set()
            for row in reader:
                if idx < len(row):
                    v = row[idx].strip()
                    if v:
                        out.add(v)
            return out
    except (OSError, UnicodeDecodeError, csv.Error):
        return None


def read_manifest_reasons(path: Path, spec: ManifestSpec) -> dict[str, str] | None:
    """{MRID: text} from a manifest whose YAML declares it a CSV with ``mrid_column``.

    The text is ``reason_column`` when declared, else the row's other cells as
    ``column: value`` pairs. None if undeclared, absent or unreadable.
    """
    if not spec.mrid_column:
        return None
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return None
            idx = _column_index(header, spec.mrid_column)
            if idx is None:
                return None
            ridx = _column_index(header, spec.reason_column) if spec.reason_column else None
            out: dict[str, str] = {}
            for row in reader:
                if idx >= len(row) or not row[idx].strip():
                    continue
                if ridx is not None:
                    text = row[ridx].strip() if ridx < len(row) else ""
                else:
                    text = "; ".join(
                        f"{header[j].strip()}: {v.strip()}"
                        for j, v in enumerate(row)
                        if j != idx and j < len(header) and v.strip()
                    )
                out[row[idx].strip()] = text
            return out
    except (OSError, UnicodeDecodeError, csv.Error):
        return None


MANIFEST_EXCERPT_BYTES = 8192


def manifest_excerpt(path: Path) -> dict:
    """The start of a manifest as text, for showing it as written."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(MANIFEST_EXCERPT_BYTES)
    except OSError:
        return {"bytes": None, "excerpt": None, "truncated": False}
    if b"\x00" in head:  # not text: offer the file, not an excerpt
        return {"bytes": size, "excerpt": None, "truncated": size > 0}
    text = head.decode("utf-8", errors="replace")
    if size > len(head) and "\n" in text:
        text = text[: text.rindex("\n") + 1]   # whole lines only
    return {"bytes": size, "excerpt": text, "truncated": size > len(head)}


def _first_match(directory: Path, pattern: str) -> Path | None:
    names = _entries(directory, recursive="/" in pattern)
    if not names:
        return None
    rx = pattern_regex(pattern)
    hits = sorted(n for n in names if rx.fullmatch(n))
    return directory / hits[0] if hits else None


def _nifti_stems(directory: Path) -> set[str] | None:
    names = _entries(directory, recursive=False)
    if names is None:
        return None
    out = set()
    for n in names:
        for suf in _NIFTI_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf):
                out.add(n[: -len(suf)])
                break
    return out


# ── One slot ──────────────────────────────────────────────────────────────────

@dataclass
class _Need:
    """One thing a subject must have in an output slot, for the 'why' text."""
    describe: Callable[[str], str]
    subjects: set[str] | None


def output_slot_needs(path: Path, rel: str, rules: list[ContentRule]) -> list[_Need]:
    """What every subject must have in one output slot, per its declared contents.

    *path* is where the output is now (staging); *rel* is where it is published,
    used only for wording. Rules that aren't per subject are ignored.
    """
    needs: list[_Need] = []
    is_file = bool(Path(rel).suffix)
    for r in rules:
        if r.pattern is None:
            if r.rows_per_subject and is_file:
                subs = read_csv_column(path, r.mrid_column) if path.is_file() else set()
                needs.append(_Need(lambda _m, rel=rel: f"a row in {rel}", subs))
        elif r.per_subject and "{MRID}" in r.pattern:
            base = path.parent if is_file else path
            loc = str(Path(rel).parent) if is_file else rel
            subs = subjects_matching(base, r.pattern) if base.is_dir() else set()
            needs.append(_Need(
                lambda m, loc=loc, p=r.pattern: f"{loc}/{p.replace('{MRID}', m)}", subs,
            ))
        elif r.rows_per_subject:
            base = path.parent if is_file else path
            loc = str(Path(rel).parent) if is_file else rel
            hit = _first_match(base, r.pattern) if base.is_dir() else None
            subs = (read_csv_column(hit, r.mrid_column) if hit else set())
            needs.append(_Need(lambda _m, loc=loc, p=r.pattern: f"a row in {loc}/{p}", subs))
    return needs


def input_slot_subjects(
    path: Path,
    rules: list[ContentRule] | None,
    producer_rules: list[ContentRule] | None,
) -> set[str] | None:
    """Subjects one input slot holds, or None when that can't be told.

    In order: the consuming tool's own ``{MRID}`` patterns for the slot, then the
    producing tool's output patterns, then — for a CSV — its MRID column, and for
    a project modality directory (``t1/``, ``fl/`` …) its ``{MRID}.nii[.gz]``
    images. Anything else (a directory of unknown naming, a non-CSV file) is
    unknown rather than empty, so it never makes a subject look missing.
    """
    if path.is_file():
        if path.suffix.lower() != ".csv":
            return None
        col = next(
            (r.mrid_column for r in (rules or []) + (producer_rules or []) if r.pattern is None),
            "MRID",
        )
        return read_csv_column(path, col)
    if not path.is_dir():
        return None
    for source in (rules, producer_rules):
        patterns = [r.pattern for r in source or [] if r.per_subject and r.pattern and "{MRID}" in r.pattern]
        if patterns:
            sets = [subjects_matching(path, p) for p in patterns]
            known = [s for s in sets if s is not None]
            return set.intersection(*known) if known else None
    if path.name in _MODALITY_DIRS:
        return _nifti_stems(path)
    return None


# ── Origins of subjects missing from a step's inputs ──────────────────────────

@dataclass
class Producer:
    """The step that wrote an input this step reads, with what it recorded."""
    step_id: str
    run_id: str | None
    pipeline_id: str | None
    tool_id: str | None
    coverage: dict | None
    rules: list[ContentRule] | None = None
    _dropped: dict[str, str] | None = field(default=None, repr=False)
    _missing: dict[str, dict] | None = field(default=None, repr=False)

    def dropped(self) -> dict[str, str]:
        if self._dropped is None:
            self._dropped = {
                d["mrid"]: d.get("reason") or ""
                for d in (self.coverage or {}).get("dropped") or []
            }
        return self._dropped

    def missing(self) -> dict[str, dict]:
        if self._missing is None:
            self._missing = {
                m["mrid"]: m.get("origin") or {}
                for m in (self.coverage or {}).get("missing_inputs") or []
            }
        return self._missing

    def ref(self) -> dict:
        return {
            "step_id": self.step_id, "run_id": self.run_id,
            "pipeline_id": self.pipeline_id, "tool_id": self.tool_id,
        }


_ORIGIN_RANK = {"dropped": 0, "not_produced": 1, "not_in_input": 2}


def origin_of(
    mrid: str,
    lacking: list[tuple[str, Producer | None]],
) -> dict:
    """Why *mrid* is absent from this step's inputs.

    *lacking* lists each input (project-relative path) whose subject set is
    known and lacks it, with the step that produced that input, if any. The most
    informative explanation wins: an upstream exclusion over "never had it".
    """
    best: dict | None = None
    for rel, prod in lacking:
        if prod is None:
            cand = {"kind": "not_in_input", "input": rel}
        elif mrid in prod.dropped():
            cand = {"kind": "dropped", **prod.ref(), "reason": prod.dropped()[mrid] or None, "input": rel}
        elif mrid in prod.missing():
            cand = dict(prod.missing()[mrid])
        else:
            cand = {"kind": "not_produced", **prod.ref(), "input": rel}
        if best is None or _ORIGIN_RANK.get(cand.get("kind"), 9) < _ORIGIN_RANK.get(best.get("kind"), 9):
            best = cand
    return best or {"kind": "not_in_input", "input": None}


# ── A step's coverage ─────────────────────────────────────────────────────────

NOTE_NO_OUTPUT_DECL = (
    "The tool's YAML doesn't declare which files it writes for each scan "
    "(outputs.<slot>.contents), so scans it skipped can't be detected."
)
NOTE_NO_INPUT_SET = "Couldn't tell which scans this step received."


def compute_coverage(
    *,
    scope: set[str] | None,
    inputs: dict[str, tuple[str, set[str] | None, Producer | None]],
    outputs: dict[str, tuple[Path, str]],
    output_rules: dict[str, list[ContentRule]],
    manifests: dict[str, ManifestSpec],
) -> dict:
    """Coverage of one finished step.

    ``inputs``: label → (project-relative path, its subjects or None, producer).
    ``outputs``: label → (where the output is now, project-relative published path).
    ``scope``: the run's subjects; None when not known.
    """
    known_inputs = [s for _rel, s, _p in inputs.values() if s is not None]
    expected = set.intersection(*known_inputs) if known_inputs else None

    needs: list[_Need] = []
    for label, (path, rel) in outputs.items():
        needs.extend(output_slot_needs(path, rel, output_rules.get(label) or []))
    known_needs = [n for n in needs if n.subjects is not None]
    produced = set.intersection(*(n.subjects for n in known_needs)) if known_needs else None  # type: ignore[misc]

    result: dict = {
        "checked": False, "note": None,
        "scope": len(scope) if scope is not None else None,
        "expected": len(expected) if expected is not None else None,
        "produced": len(produced) if produced is not None else None,
        "unexpected": 0, "dropped": [], "missing_inputs": [],
        "manifests": [], "all_excluded": False,
    }

    # The tool's manifests, recorded whatever their format, plus per-subject
    # text from those whose YAML says how to read them.
    reasons: dict[str, str] = {}
    for label, spec in manifests.items():
        if label not in outputs:
            continue
        path, rel = outputs[label]
        base = path.parent if Path(rel).suffix else path
        hit = _first_match(base, spec.pattern) if base.is_dir() else None
        if hit is None:
            continue
        loc = str(Path(rel).parent) if Path(rel).suffix else rel
        result["manifests"].append(
            {"path": f"{loc}/{hit.relative_to(base).as_posix()}", **manifest_excerpt(hit)}
        )
        for mrid, why in (read_manifest_reasons(hit, spec) or {}).items():
            reasons.setdefault(mrid, why)

    if produced is None:
        result["note"] = NOTE_NO_OUTPUT_DECL
        return result
    if expected is None:
        result["note"] = NOTE_NO_INPUT_SET
        return result
    result["checked"] = True

    dropped = sorted(expected - produced)
    for mrid in dropped:
        if mrid in reasons:
            result["dropped"].append({
                "mrid": mrid, "source": "manifest",
                "reason": reasons[mrid] or "Listed in the tool's manifest",
            })
        else:
            missing = [n.describe(mrid) for n in known_needs if mrid not in n.subjects]  # type: ignore[operator]
            result["dropped"].append({
                "mrid": mrid, "source": "no_output",
                "reason": "No output: missing " + ", ".join(missing),
            })
    result["unexpected"] = len(produced - expected)

    if scope is not None:
        for mrid in sorted(scope - expected - produced):
            lacking = [
                (rel, prod) for rel, subs, prod in inputs.values()
                if subs is not None and mrid not in subs
            ]
            result["missing_inputs"].append({"mrid": mrid, "origin": origin_of(mrid, lacking)})

    result["all_excluded"] = not (produced & expected) and (
        len(expected) > 0 or bool(scope)
    )
    return result


def incomplete_subjects(coverages: list[dict | None], scope: set[str] | None) -> set[str]:
    """Subjects some step dropped or never received: those without a full result."""
    out: set[str] = set()
    for cov in coverages:
        if not cov or not cov.get("checked"):
            continue
        out.update(d["mrid"] for d in cov.get("dropped") or [])
        out.update(m["mrid"] for m in cov.get("missing_inputs") or [])
    return out & scope if scope is not None else out
