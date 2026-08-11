"""
Central registry of imaging modalities — the single source of truth.

Everything modality-related derives from ``MODALITIES`` below: the study
subdirectory a modality's images live in (``${STUDY}/{code}/``), filename- and
BIDS-based inference, MRID-suffix stripping, the ``needs_<code>`` pipeline
requirement keywords, the CLI/MCP inputs, and the ``GET /catalog/modalities``
endpoint.

**To add a modality** (e.g. PET), add ONE ``Modality(...)`` entry here. Put more
specific tokens before ones they'd otherwise collide with (``t1ce`` before
``t1``). Nothing else needs editing — file uploads, readiness (``needs_pet``),
the CLI (``--image pet=…``), the MCP tool, and the catalog endpoint all pick it
up automatically. (An optional convenience: a named CLI flag like ``--pet`` can
be added in one line, but ``--image`` already works without it.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Modality:
    """One imaging modality. ``code`` is the canonical id, the study subdirectory
    name, and the key used everywhere (CLI, API, filenames)."""

    code: str
    label: str
    # Regex (case-insensitive) that marks this modality in an upload filename.
    filename_regex: str
    # Literal suffix tokens stripped from a filename to derive the MRID.
    strip_tokens: tuple[str, ...]
    # BIDS-suffix regex, if it differs from ``filename_regex`` (e.g. ``_T1w``).
    bids_regex: str | None = None
    # Extra ``needs_*`` requirement aliases beyond the implicit ``needs_<code>``.
    requires_aliases: tuple[str, ...] = ()

    @property
    def dir_name(self) -> str:
        return self.code


# Order matters for inference: a more specific token must come before one it
# contains (t1ce before t1). MRID stripping is order-independent (longest-first).
MODALITIES: tuple[Modality, ...] = (
    Modality("t1ce", "T1 post-contrast (T1CE)", r"_T1CE", ("_T1CE",)),
    Modality("fl",   "FLAIR",                    r"_(?:FLAIR|FL)\b", ("_FLAIR", "_FL"),
             requires_aliases=("needs_flair",)),
    Modality("t2",   "T2-weighted",              r"_T2\b", ("_T2",), bids_regex=r"_T2w?\b"),
    Modality("adc",  "ADC",                      r"_ADC\b", ("_ADC",)),
    Modality("t1",   "T1-weighted",              r"_T1(?:w)?\b", ("_T1w", "_T1"),
             bids_regex=r"_T1w?\b", requires_aliases=("needs_t1w",)),
    Modality("pet",  "PET",                      r"_PET\b", ("_PET",)),
)

# ── Derived lookups ───────────────────────────────────────────────────────────

BY_CODE: dict[str, Modality] = {m.code: m for m in MODALITIES}
#: Canonical codes, in inference order.
MODALITY_CODES: tuple[str, ...] = tuple(m.code for m in MODALITIES)
CODES: frozenset[str] = frozenset(MODALITY_CODES)

_FILENAME_RE = [(m.code, re.compile(m.filename_regex, re.IGNORECASE)) for m in MODALITIES]
_BIDS_RE = [(m.code, re.compile(m.bids_regex or m.filename_regex, re.IGNORECASE)) for m in MODALITIES]

# One optional modality suffix immediately before the extension, longest token
# first so e.g. "_T1CE"/"_T1w" win over "_T1".
_ALL_STRIP_TOKENS = sorted(
    {t for m in MODALITIES for t in m.strip_tokens}, key=len, reverse=True
)
_STRIP_SUFFIX = re.compile(
    r"(?:" + "|".join(re.escape(t) for t in _ALL_STRIP_TOKENS) + r")?(\.nii\.gz|\.nii)$",
    re.IGNORECASE,
)

#: ``needs_<code>`` (and aliases) → modality code.
REQUIRES_TOKENS: dict[str, str] = {}
for _m in MODALITIES:
    REQUIRES_TOKENS[f"needs_{_m.code}"] = _m.code
    for _alias in _m.requires_aliases:
        REQUIRES_TOKENS[_alias.lower()] = _m.code


# ── Public helpers ────────────────────────────────────────────────────────────

def is_valid(code: str) -> bool:
    """True if ``code`` is a known modality code."""
    return code in CODES


def infer_modality(filename: str) -> str | None:
    """Infer a modality code from an upload filename, or None."""
    for code, pattern in _FILENAME_RE:
        if pattern.search(filename):
            return code
    return None


def infer_modality_bids(filename: str) -> str | None:
    """Infer a modality code from a BIDS filename suffix, or None."""
    for code, pattern in _BIDS_RE:
        if pattern.search(filename):
            return code
    return None


def strip_to_mrid(filename: str) -> str:
    """Derive the MRID from a NIfTI filename (strip one modality suffix + extension)."""
    return _STRIP_SUFFIX.sub("", filename).strip("_-. ")


def modality_for_requires(token: str) -> str | None:
    """Map a ``requires`` keyword (e.g. 'needs_flair') to a modality code, or None."""
    return REQUIRES_TOKENS.get(token.lower())


def catalog() -> list[dict[str, str]]:
    """Serializable [{code, label, dir}] list for the catalog endpoint / clients."""
    return [{"code": m.code, "label": m.label, "dir": m.dir_name} for m in MODALITIES]
