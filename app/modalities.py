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
#: Token boundary: the modality token must not run into more letters or digits,
#: but a following separator is fine. ``\b`` cannot express this, because ``_``
#: is a word character and so ``_T1\b`` does not match ``sub01_T1_brain``.
_END = r"(?![A-Za-z0-9])"

#: Separator the modality token must be preceded by. The brief's naming survey
#: found both ``sub01_T1`` and ``sub01-T1`` in the wild; a token at position 0
#: has no separator before it, so ``T1_sub01`` is not read as a T1 marker.
_SEP = r"[-_.]"

MODALITIES: tuple[Modality, ...] = (
    Modality("t1ce", "T1 post-contrast (T1CE)", _SEP + r"T1CE" + _END, ("_T1CE",)),
    Modality("fl",   "FLAIR",                    _SEP + r"(?:FLAIR|FL)" + _END, ("_FLAIR", "_FL"),
             requires_aliases=("needs_flair",)),
    Modality("t2",   "T2-weighted",              _SEP + r"T2" + _END, ("_T2",),
             bids_regex=_SEP + r"T2w?" + _END),
    Modality("adc",  "ADC",                      _SEP + r"ADC" + _END, ("_ADC",)),
    Modality("t1",   "T1-weighted",              _SEP + r"T1(?:w)?" + _END, ("_T1w", "_T1"),
             bids_regex=_SEP + r"T1w?" + _END, requires_aliases=("needs_t1w",)),
    Modality("pet",  "PET",                      _SEP + r"PET" + _END, ("_PET",)),
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

#: Characters that separate tokens in an upload filename.
SEPARATORS = "_-."

_NIFTI_EXT = re.compile(r"\.nii(?:\.gz)?$", re.IGNORECASE)

# Modality tokens without their leading separator, longest first so "T1CE"/"T1w"
# win over "T1". The trailing lookahead keeps "_T1" from matching inside "_T1w".
_BARE_STRIP_TOKENS = sorted(
    {t.lstrip(SEPARATORS) for m in MODALITIES for t in m.strip_tokens},
    key=len,
    reverse=True,
)
_MODALITY_TOKEN = re.compile(
    r"[" + re.escape(SEPARATORS) + r"](?:"
    + "|".join(re.escape(t) for t in _BARE_STRIP_TOKENS)
    + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# A run of separator-led tokens, e.g. "_T1_brain" or "-FLAIR-reg".
_TOKEN_RUN = re.compile(r"(?:[" + re.escape(SEPARATORS) + r"][A-Za-z0-9]+)+")

#: Upper bound on how many separator-led tokens a batch-wide shared suffix may
#: span before it is rejected as probably part of the ID rather than a suffix.
#: Two covers "_T1", "_T1_brain", "-FLAIR-reg" and site tags like "_MPRAGE".
#: Three would strip "-Guys-0828-T1" off a two-file IXI batch, taking the site
#: and session with it; the per-file rule handles longer modality-led endings.
MAX_SHARED_SUFFIX_TOKENS = 2

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


def nifti_stem(filename: str) -> str:
    """The filename with only its ``.nii``/``.nii.gz`` extension removed."""
    return _NIFTI_EXT.sub("", filename)


def strip_to_mrid(filename: str) -> str:
    """Derive the MRID from a single NIfTI filename.

    Cuts at the **last** modality token that is preceded by a separator and
    followed only by further separator-led tokens, so ``sub01_T1_brain`` and
    ``sub01-FLAIR-reg`` both reduce to ``sub01`` while ``sub_T1_x_T2_y`` cuts at
    ``_T2`` rather than ``_T1``. A token at position 0 has no preceding
    separator and is left alone, so ``T1_sub01`` is unchanged.

    This is the per-file rule. It sees one name at a time and therefore cannot
    recognise a site tag like ``_MPRAGE``; a batch-wide shared suffix is handled
    by :func:`shared_suffix_cut`.
    """
    stem = nifti_stem(filename)
    cut = _modality_cut(stem)
    return (stem if cut is None else stem[:cut]).strip("_-. ")


def _modality_cut(stem: str) -> int | None:
    """Index to cut *stem* at, or None if it carries no strippable modality token."""
    for match in reversed(list(_MODALITY_TOKEN.finditer(stem))):
        if _TOKEN_RUN.fullmatch(stem[match.start():]):
            return match.start()
    return None


def _longest_common_suffix(values: list[str]) -> str:
    common = values[0]
    for value in values[1:]:
        n = 0
        while n < min(len(common), len(value)) and common[-1 - n] == value[-1 - n]:
            n += 1
        common = common[len(common) - n:] if n else ""
        if not common:
            break
    return common


def shared_suffix_cut(stems: list[str]) -> str | None:
    """The trailing string that every stem in a batch shares, or None.

    Finds the longest common suffix of *stems*, then moves the cut forward to
    the first separator inside it, so a suffix that starts mid-token (``1_T1``
    shared by ``sub001_T1``/``sub011_T1``) becomes ``_T1``. Returns None when
    there are fewer than two stems, when nothing is shared, when the shared part
    contains no separator (``abcT1``/``defT1`` share ``T1``, which is not a
    token boundary), or when the cut spans more than
    :data:`MAX_SHARED_SUFFIX_TOKENS` tokens.
    """
    if len(stems) < 2:
        return None
    common = _longest_common_suffix(stems)
    if not common:
        return None
    first_sep = next((i for i, ch in enumerate(common) if ch in SEPARATORS), None)
    if first_sep is None:
        return None
    cut = common[first_sep:]
    if len(re.findall(r"[" + re.escape(SEPARATORS) + r"][^" + re.escape(SEPARATORS) + r"]*", cut)) > MAX_SHARED_SUFFIX_TOKENS:
        return None
    return cut


def apply_shared_suffix(stems: list[str]) -> dict[str, str] | None:
    """Map each stem to its MRID by removing the batch's shared suffix.

    Returns None — meaning the caller should fall back to the per-file rule for
    the whole group — if there is no shared suffix, if removing it would leave
    any MRID empty, or if two stems would collapse to the same MRID. The cut is
    all-or-nothing: a partially applied suffix would produce exactly the
    inconsistent subject sets this is meant to prevent.
    """
    cut = shared_suffix_cut(stems)
    if cut is None:
        return None
    out: dict[str, str] = {}
    for stem in stems:
        if not stem.endswith(cut):
            return None
        mrid = stem[: -len(cut)].strip("_-. ")
        if not mrid:
            return None
        out[stem] = mrid
    if len(set(out.values())) != len(out):
        return None
    return out


def modality_for_requires(token: str) -> str | None:
    """Map a ``requires`` keyword (e.g. 'needs_flair') to a modality code, or None."""
    return REQUIRES_TOKENS.get(token.lower())


def catalog() -> list[dict[str, str]]:
    """Serializable [{code, label, dir}] list for the catalog endpoint / clients."""
    return [{"code": m.code, "label": m.label, "dir": m.dir_name} for m in MODALITIES]
