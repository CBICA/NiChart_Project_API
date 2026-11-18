# SurrealGAN Disease Subtypes

## Links

- **Paper:** [Surreal-GAN: Semi-Supervised Representation Learning via GAN for Uncovering Heterogeneous Disease-Related Imaging Patterns](https://arxiv.org/abs/2205.04523) (Yang, Wen & Davatzikos, *ICLR*, 2022)
- **GitHub (core method):** [CBICA/SurrealGAN](https://github.com/CBICA/SurrealGAN)
- **GitHub (prediction tool used by this pipeline):** [CBICA/PredCRD](https://github.com/CBICA/PredCRD)

SurrealGAN identifies individualized, continuous disease subtypes from structural MRI features without requiring diagnostic labels. It learns a low-dimensional representation of heterogeneous brain change patterns, enabling researchers to characterize participant-level disease expression rather than assigning discrete categories.

## What this pipeline produces

- **R-scores CSV** — one row per subject, one column for each of the 5 learned disease dimensions. Each R-score reflects how strongly a subject's brain pattern aligns with a particular mode of neurodegeneration.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory. The pipeline runs DLMUSE segmentation internally to get the regional volumes.
- `participants/participants.csv` with columns: `MRID`, `Age` (0–120), `Sex` (M or F)

## Note

In NiChart, the indices are computed by a tabular transformer trained through knowledge distillation to reproduce the original model's outputs.
