# DKGP

## Links

- **GitHub:** [CBICA/NiChart_DKGP](https://github.com/CBICA/NiChart_DKGP)
- **Website:** [NiChart Platform](https://neuroimagingchart.com/)

DKGP (Deep Kernel Gaussian Process) combines deep neural networks with Gaussian Processes to model population-level biomarker trajectories over time. It provides multi-year trajectory forecasts with uncertainty quantification (95% posterior predictive confidence intervals) for individual subjects. In this pipeline, DKGP is applied to DLMUSE regional brain volumes, demographics, and SPARE scores to forecast each subject's biomarker trajectory.

## What this pipeline produces

- **Trajectory forecasts** — per-subject, multi-year biomarker trajectory predictions with confidence intervals, written to the pipeline's output directory.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, in the project's `t1/` directory.
- `participants/participants.csv` with columns: `MRID`, `Age`, `Sex`, `AD_Diagnosis` (CN/MCI/AD), `ADAS_COG_13`, `MMSE`.
- The pipeline runs DLMUSE segmentation and SPARE-All scoring internally as prerequisite steps.
