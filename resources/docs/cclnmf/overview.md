# CCL-NMF Brain Subtypes

## Links

- **Paper:** [Coupled cross-sectional and longitudinal non-negative matrix factorization reveals dominant brain aging trajectories in 48,949 individuals](https://www.nature.com/articles/s41467-026-72091-7) (Skampardoni et al., *Nature Communications*, 2026)
- **GitHub:** [CBICA/CCL_NMF_Prediction](https://github.com/CBICA/CCL_NMF_Prediction)

CCL-NMF (Coupled Cross-sectional and Longitudinal Non-negative Matrix Factorization) discovers interpretable brain subtypes by jointly decomposing cross-sectional and longitudinal structural MRI features into non-negative loading patterns. Coupling the two data modes produces subtypes that are both anatomically interpretable and stable across time, capturing the gradual, overlapping nature of neurodegeneration. Each subject receives continuous membership scores for each discovered subtype rather than a hard cluster assignment.

## What this pipeline produces

- **Prediction loadings CSV** — one row per subject, one column per NMF component. Values represent each subject's degree of membership in each brain subtype pattern.

## Input requirements

- DLMUSE regional brain volumes CSV (produced by the DLMUSE pipeline)
- `participants/participants.csv` with at minimum an `MRID` column
