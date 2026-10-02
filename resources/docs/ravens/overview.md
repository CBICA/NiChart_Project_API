# RAVENS Maps

## Links

- **Paper:** [Voxel-Based Morphometry Using the RAVENS Maps: Methods and Validation Using Simulated Longitudinal Atrophy](https://pubmed.ncbi.nlm.nih.gov/11707092/) (Davatzikos et al., *NeuroImage*, 2001)
- **GitHub:** not yet publicly available

RAVENS (Regional Analysis of Volumes Examined in Normalized Space) is a voxel-based morphometry method that spatially normalizes brain images using an elastic, volume-preserving deformation, so that local tissue density in the normalized image reflects the original regional tissue volume. This pipeline combines DLMUSE segmentation with the RAVENS transform to generate patient-specific regional volume maps, enabling detection of localized brain volume abnormalities relative to a reference population.

## What this pipeline produces

- **RAVENS maps** — per-subject NIfTI volume maps in normalized space, one per tissue class, suitable for voxel-wise abnormality analysis.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory.
- `participants/participants.csv` with columns: `MRID`, `Age`, `Sex`.
