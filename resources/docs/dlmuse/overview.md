# DLMUSE Brain Segmentation

DLMUSE (Deep Learning Multi-atlas Segmentation) automatically parcellates T1-weighted MRI brain scans into hundreds of anatomical regions and computes their volumes. It combines a deep learning intracranial volume extraction step (DLICV) with multi-atlas label fusion to produce both fine-grained regional volumes and a full segmentation image.

## What this pipeline produces

- **Regional volume CSV** — one row per subject, one column per brain region (e.g. `DL_MUSE_Volume_47` for the right hippocampus). Volumes are in mm³.
- **Segmentation NIfTI** — a labeled image in the same space as the input T1, suitable for overlay visualization.

## Harmonized variant

The harmonized variant (`DLMUSE Brain Segmentation — Harmonized`) adds a ComBat-FAM harmonization step after volume extraction. This corrects for site and scanner effects, making volumes more comparable across studies. Use the harmonized outputs as inputs to downstream SPARE score pipelines when your data comes from multiple sites or acquisition protocols.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, named `{MRID}.nii.gz` and placed in the project's `t1/` directory.
- No participants.csv is required for this pipeline.

## References

Bashyam V, et al. (2023). DLMUSE: Robust Brain Segmentation in Seconds Using Deep Learning. *Radiology: Artificial Intelligence*.
