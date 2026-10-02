# AmyloidPETNet

## Links

- **Paper:** [AmyloidPETNet: Classification of Amyloid Positivity in Brain PET Imaging Using End-to-End Deep Learning](https://pmc.ncbi.nlm.nih.gov/articles/PMC11211958/) (*Radiology*, 2024; DOI: 10.1148/radiol.231442)
- **GitHub:** [sotiraslab/AmyloidPETNet](https://github.com/sotiraslab/AmyloidPETNet)

AmyloidPETNet is an end-to-end deep learning model that classifies amyloid positivity directly from minimally processed brain PET scans, without requiring a companion structural MRI. It was trained and validated on over 8,400 native-space PET scans across five tracers and five independent datasets, generalizing well to previously unseen tracers and sites (AUC ≥ 0.95).

## What this pipeline produces

- **Predictions CSV** (`predictions.csv`) — one row per subject with columns `MRID`, `img_path`, and `y_score` (the predicted amyloid-positivity score).

## Input requirements

- PET scans in NIfTI format (`.nii.gz`), provided as a flat directory. No structural MRI is required.
