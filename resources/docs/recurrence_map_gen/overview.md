# Glioblastoma Recurrence Map

## Links

- **Paper:** [Cancer Imaging Phenomics via CaPTk: Multi-Institutional Prediction of Progression-Free Survival and Pattern of Recurrence in Glioblastoma](https://doi.org/10.1200/CCI.19.00121) (Bakas et al., *JCO Clinical Cancer Informatics*, 2020)
- **Related validation study:** [Multi-institutional validation of an AI-based model for prediction of tumor infiltration and future recurrence in glioblastoma (ReSPOND consortium)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11553728/)
- **Toolkit documentation:** [CaPTk (Cancer Imaging Phenomics Toolkit)](https://cbica.github.io/CaPTk)
- **GitHub:** not yet publicly available for this exact pipeline

This pipeline generates spatial maps predicting glioblastoma recurrence and tumor infiltration from preoperative multiparametric MRI. It builds on CBICA's Cancer Imaging Phenomics methodology (as implemented in CaPTk), which combines multi-institutional structural and diffusion MRI to predict progression-free survival and the spatial pattern of subsequent recurrence.

## What this pipeline produces

- **Recurrence/infiltration maps** — per-subject spatial probability maps of predicted tumor recurrence location, written to the pipeline's output directory.

## Input requirements

- All five scan modalities are required, one file per subject in each of the project's `t1/`, `t1ce/`, `t2/`, `fl/`, and `adc/` directories:
  - T1-weighted
  - T1-weighted post-contrast (T1CE)
  - T2-weighted
  - FLAIR
  - ADC (apparent diffusion coefficient)
