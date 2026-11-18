# DLWMLS White Matter Lesion Segmentation

## Links

- **GitHub:** [CBICA/DLWMLS](https://github.com/CBICA/DLWMLS)
- **Underlying architecture:** [nnU-Net: a self-configuring method for deep learning-based biomedical image segmentation](https://pubmed.ncbi.nlm.nih.gov/33288961/) (Isensee et al., *Nature Methods*, 2021) — DLWMLS is a trained nnU-Net model.

DLWMLS quantifies white matter lesion burden from paired T1-weighted and FLAIR MRI scans. It uses a deep learning segmentation model trained on multi-site clinical data and combines the lesion outputs with DLMUSE cortical and subcortical volumes into a merged feature CSV suitable for downstream analysis.

## What this pipeline produces

- **Regional lesion volume CSV** (`nichart_dlwmls_out/DLWMLS_DLMUSE_Segmented_Volumes.csv`) — white matter lesion volume within each DLMUSE region, one row per subject.
- **Merged feature CSV** — DLMUSE regional volumes (harmonized, in the harmonized variant) combined with the lesion volumes, one row per subject.
- **Lesion mask NIfTI** — a binary mask of detected white matter lesions, in FLAIR space.
- **Lesions by region NIfTI** — the lesions labeled by DLMUSE region, in T1 space.

## Harmonized variant

The harmonized variant applies ComBat-FAM harmonization to the DLMUSE volumes before merging with lesion features. Use this variant when your data spans multiple sites or when combining outputs with harmonized SPARE pipelines.

## Input requirements

- T1-weighted scans in `t1/` and FLAIR scans in `fl/`, both named `{MRID}.nii.gz`.
- `participants/participants.csv` with an `MRID` column (created automatically when images are uploaded).
- The harmonized variant requires `participants/participants.csv` with columns `MRID`, `Age` (0–120), `Sex` (M or F) and `DX`, and at least 30 scans, including at least 30 cognitively normal (`DX` = `CN`) scans in each `Batch`.
