# DLWMLS White Matter Lesion Segmentation

DLWMLS quantifies white matter lesion burden from paired T1-weighted and FLAIR MRI scans. It uses a deep learning segmentation model trained on multi-site clinical data and combines the lesion outputs with DLMUSE cortical and subcortical volumes into a merged feature CSV suitable for downstream analysis.

## What this pipeline produces

- **Merged feature CSV** — regional DLMUSE volumes combined with DLWMLS lesion volumes, one row per subject. Used as input to CVM SPARE score pipelines.
- **Lesion segmentation NIfTI** — a binary mask of detected white matter lesions in the FLAIR space.

## Harmonized variant

The harmonized variant applies ComBat-FAM harmonization to the DLMUSE volumes before merging with lesion features. Use this variant when your data spans multiple sites or when combining outputs with harmonized SPARE pipelines.

## Input requirements

- T1-weighted scans in `t1/` and FLAIR scans in `fl/`, both named `{MRID}.nii.gz`.
- `participants/participants.csv` with columns: `MRID`, `Age` (numeric), `Sex` (M or F).
