# GenFAR

## Links

- **Paper:** [GenFAR: A generalized representation of brain structure, derived from 49,246 multi-cohort MRIs via deep learning](https://arxiv.org/abs/2608.12185) (arXiv)
- **GitHub:** not yet publicly available

GenFAR is a modular deep learning framework that learns generalized, clinically informed features of brain structure from T1-weighted MRI. It was trained across 49,246 individuals from 11 cohorts on 17 classification and regression tasks spanning cognition, diagnosis, demographics, and biomarkers, using a sequential learning strategy where each task builds on representations learned from prior tasks. The resulting features are intended to transfer to downstream tasks beyond those used in training.

## What this pipeline produces

- **GenFAR feature/biomarker outputs** written to the pipeline's output directory, derived from MNI ICBM152-aligned T1 scans.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory.
- No participants.csv is required. The pipeline runs DLMUSE segmentation and MNI ICBM152 alignment internally as prerequisite steps.
