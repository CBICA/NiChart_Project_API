# Brain2Vec Embedding Extraction

## Links

- **Paper:** [Brain2Vec: A Self-Supervised 3D Foundation Model for Structural Brain MRI](https://arxiv.org/abs/2610.28411) (Maggioros et al., arXiv, 2026)
- **GitHub:** [CBICA/Brain2Vec](https://github.com/CBICA/Brain2Vec)
- **Website:** [NiChart Platform](https://neuroimagingchart.com/)

Brain2Vec extracts latent brain embeddings from T1-weighted MRI scans using a masked autoencoder (ViT-Base, patch size 8). Rather than computing hand-designed regional volumes, it encodes each scan directly into a compact, learned representation intended for downstream models — brain-age prediction, disease classification, or other phenotypic inference tasks.

## What this pipeline produces

- **Embeddings CSV** — one row per subject with columns `MRID`, `feature_0000` … `feature_0767` (a 768-dimensional feature vector).
- **Embeddings JSON** — the same raw embedding vectors, for programmatic downstream use.

## Input requirements

- T1-weighted MRI scans in NIfTI format (`.nii.gz`), one file per subject, named `{MRID}.nii.gz` and placed in the project's `t1/` directory.
- No participants.csv is required for this pipeline.
