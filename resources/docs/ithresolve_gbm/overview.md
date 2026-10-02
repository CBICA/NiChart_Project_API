# ITHResolve-GBM

## Links

- **Paper:** [A cellular epigenetic classification system for glioblastoma](https://pmc.ncbi.nlm.nih.gov/articles/PMC13128495/) (*Neuro-Oncology*, 2026; DOI: 10.1093/neuonc/noaf299)
- **GitHub:** not yet publicly available

ITHResolve-GBM (ITHresolveGBM) is a hierarchical non-negative matrix factorization method that deconvolutes bulk DNA methylation profiles from glioblastoma tissue. It infers the relative abundance of microenvironmental cell types (glial, immune, neuronal) and further distinguishes the differentiation states of malignant cells, resolving intratumoral heterogeneity (ITH) directly from Illumina methylation array data.

## What this pipeline produces

- **Tumor composition scores** — per-subject methylation-class/cell-type composition measures, written to the pipeline's output directory.

## Input requirements

- IDAT epigenetic array files (Illumina methylation arrays), placed in the project's `idat/` directory.
- No participants.csv is required for this pipeline.
