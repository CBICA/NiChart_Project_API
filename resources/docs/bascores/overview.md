# DL-BA (Deep Learning Brain Age)

## Links

- **GitHub:** [CBICA/NiChart_BAScores](https://github.com/CBICA/NiChart_BAScores)
- **Website:** [NiChart Cloud](https://cloud.neuroimagingchart.com)
- **Related:** [CBICA Brain AGE program](https://www.med.upenn.edu/cbica/brain-age.html)

DL-BA (DeepSPARE-BA) is an image-to-biomarker brain age model that regresses brain age directly from MNI-aligned T1-weighted MRI, without first computing intermediate regional volumes. Alongside the predicted age it produces attention maps highlighting the regions driving each prediction, making the score interpretable rather than a single opaque number.

## What this pipeline produces

- **Brain age CSV** (`SPARE_BA_Image.csv`) — one row per subject with the predicted brain age. The gap between predicted and chronological age (the brain-age gap, BAG) reflects accelerated or decelerated brain aging.
- **Attention maps** — per-subject saliency images indicating which brain regions most influenced the prediction.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory.
- No participants.csv is required. The pipeline runs DLMUSE segmentation and MNI alignment internally as prerequisite steps.
