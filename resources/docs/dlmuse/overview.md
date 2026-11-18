# DLMUSE Brain Segmentation

## Links

- **Paper:** [DLMUSE: Robust Brain Segmentation in Seconds Using Deep Learning](https://pubmed.ncbi.nlm.nih.gov/40960397/) (Bashyam et al., *Radiology: Artificial Intelligence*, 2025)
- **GitHub:** [CBICA/DLMUSE](https://github.com/CBICA/DLMUSE)
- **Documentation:** [NiChart_DLMUSE](https://cbica.github.io/NiChart_DLMUSE/)

DLMUSE (Deep Learning Multi-atlas Segmentation) automatically parcellates T1-weighted MRI brain scans into hundreds of anatomical regions and computes their volumes. It combines a deep learning intracranial volume extraction step (DLICV) with multi-atlas label fusion to produce both fine-grained regional volumes and a full segmentation image.

## What this pipeline produces

- **Regional volume CSV** — one row per subject, one column per brain region (e.g. `DL_MUSE_Volume_47` for the right hippocampus). Volumes are in mm³.
- **Segmentation NIfTI** — a labeled image in the same space as the input T1, suitable for overlay visualization.

## Harmonized variant

The harmonized variant (`DLMUSE (Harmonized)`) adds a ComBat-FAM harmonization step after volume extraction. This corrects for site and scanner effects, making volumes more comparable across studies. Its volumes are written to `dlmuse_vol_harmonized/`, with columns named `H_DL_MUSE_Volume_{id}`. The harmonized SPARE and DLWMLS pipelines run this same step themselves.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, named `{MRID}.nii.gz` and placed in the project's `t1/` directory.
- No participants.csv is required for the standard pipeline.
- The harmonized variant requires `participants/participants.csv` with columns `MRID`, `Age` (0–120), `Sex` (M or F) and `DX`, and at least 30 scans, including at least 30 cognitively normal (`DX` = `CN`) scans in each `Batch`.
