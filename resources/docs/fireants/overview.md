# FireANTs MNI Registration

## Links

- **Paper:** [FireANTs: Adaptive Riemannian Optimization for Multi-Scale Diffeomorphic Matching](https://arxiv.org/abs/2404.01249) (Jena et al., arXiv)
- **GitHub:** [rohitrango/FireANTs](https://github.com/rohitrango/FireANTs)

FireANTs is a GPU-accelerated image registration library that implements the classic ANTs-style registration cascade (Rigid → Affine → SyN) with adaptive optimization, running orders of magnitude faster than CPU-based registration. This pipeline uses it to align each subject's T1-weighted scan to the MNI152 template, placing all subjects in a common space for template-based analyses.

## What this pipeline produces

- **Warped T1 image** (`fireants_mni/{MRID}_warped.nii.gz`) — the subject's T1 resampled into MNI152 template space.
- **SyN warp field** (`fireants_mni/{MRID}_0Warp.nii.gz`) — the estimated deformable transform, usable downstream for template-space analyses and RAVENS-style workflows.

## Registration options

The `transform_type` parameter selects the registration cascade:

- `rigid` — Rigid only (rotation and translation).
- `affine` — Rigid → Affine (adds scaling and shear).
- `syn` — Rigid → Affine → SyN deformable registration (default).

A warp field is only meaningful for the `syn` option.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, named `{MRID}.nii.gz` and placed in the project's `t1/` directory.
- No participants.csv is required for this pipeline.
- Requires a GPU.
