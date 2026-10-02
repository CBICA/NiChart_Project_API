# SynthSeg

## Links

- **Paper:** [SynthSeg: Segmentation of brain MRI scans of any contrast and resolution without retraining](https://arxiv.org/abs/2107.09559) (Billot et al., *Medical Image Analysis*, 2023)
- **GitHub:** [BBillot/SynthSeg](https://github.com/BBillot/SynthSeg)

SynthSeg is a convolutional neural network for whole-brain MRI segmentation that works out-of-the-box on scans of any contrast and resolution, without retraining or fine-tuning. It is trained entirely on synthetic data generated with fully randomized contrast and resolution, making it robust across acquisition protocols, scanner types, and subject populations — including scans with pathology. This pipeline runs SynthSeg on T1-weighted scans, though the underlying model accepts any single MRI modality.

## What this pipeline produces

- **Segmentation NIfTI** — a whole-brain label map in the same space as the input scan.
- **Regional volume CSV** (`volumes.csv`) — one row per subject, one column per segmented region, in mm³.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory.
- No participants.csv is required for this pipeline.
